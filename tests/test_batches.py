import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.audit import AuditRecorder
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import DomainRules
from src.service import Service


TRADER = Actor("trader-1", "trader")
OFFICER = Actor("officer-1", "settlement_officer")
SUP_A = Actor("sup-a", "settlement_supervisor")
SUP_B = Actor("sup-b", "settlement_supervisor")
CA_TEAM = Actor("ca-1", "corporate_actions")

BATCH_REQ = {"batch_no": "BN-1", "account": "ACC-1", "currency": "CNY", "settlement_day": 2}


def instruction(instrument="ACME", side="buy", quantity=1000, price=12.5, fees=18.0, currency="CNY", day=2, ca="split", ratio=2.0, account="ACC-1"):
    return {
        "instrument": instrument,
        "side": side,
        "quantity": quantity,
        "price": price,
        "fees": fees,
        "currency": currency,
        "settlement_day": day,
        "corporate_action": ca,
        "action_ratio": ratio,
        "account": account,
    }


class CrashOnceRepository(Repository):
    """模拟写入明细中途崩溃：事务应整体回滚，不留半张批次。"""

    def __init__(self, db_path):
        super().__init__(db_path)
        self.crash_armed = True

    def _insert_batch_items(self, connection, batch_id, items, now):
        if self.crash_armed:
            self.crash_armed = False
            raise RuntimeError("模拟写入明细时崩溃")
        super()._insert_batch_items(connection, batch_id, items, now)


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_net_batch_groups_by_account_currency_day(self):
        self.service.create(TRADER, "TRD-1", instruction(quantity=1000))
        self.service.create(TRADER, "TRD-2", instruction(side="sell", quantity=400, price=12.6, ca="none", ratio=1.0))
        self.service.create(TRADER, "TRD-3", instruction(quantity=50, account="ACC-2", ca="none", ratio=1.0))
        self.service.create(TRADER, "TRD-4", instruction(quantity=60, currency="USD", ca="none", ratio=1.0))
        self.service.create(TRADER, "TRD-5", instruction(quantity=70, day=3, ca="none", ratio=1.0))
        view = self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        self.assertFalse(view["replayed"])
        self.assertTrue(view["verification"]["ok"])
        self.assertEqual(len(view["items"]), 2)
        # 同标的指令统一采用版本化比例：TRD-1 收 2000 股，TRD-2 付 800 股（400×2.0）
        self.assertEqual(view["batch"]["totals"], {"ACME": 1200})
        self.assertEqual(view["batch"]["net_cash"], round(-12518.0 + 5022.0, 2))
        references = sorted(item["record_id"] for item in view["items"])
        self.assertEqual(len(set(references)), 2)

    def test_confirm_freezes_and_unbatched_recalculated(self):
        self.service.create(TRADER, "TRD-1", instruction(quantity=1000))
        view = self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        self.service.create(TRADER, "TRD-2", instruction(quantity=500, price=13.0))
        correction = self.service.register_corporate_action(CA_TEAM, {"instrument": "ACME", "action_type": "split", "ratio": 3.0, "note": "交收前修正"})
        self.assertEqual(correction["ca_version"]["version"], 2)
        self.assertEqual([item["reference"] for item in correction["unbatched"]], ["TRD-2"])
        self.assertEqual(correction["unbatched"][0]["ca_version"], 2)
        self.assertEqual(correction["unbatched"][0]["net_quantity"], 1500)
        confirmed = self.service.confirm_batch(SUP_A, view["batch"]["id"], view["batch"]["version"])
        self.assertEqual(confirmed["batch"]["state"], "confirmed")
        self.assertEqual(confirmed["batch"]["confirmed_by"], "sup-a")
        # 确认冻结：批内指令仍采用版本1与净收2000股，不受修正影响
        self.assertEqual(confirmed["items"][0]["ca_version"], 1)
        self.assertEqual(confirmed["items"][0]["net_quantity"], 2000)
        # 未入批指令入新批次时按新版本重算
        follow_up = self.service.generate_batch(OFFICER, {"batch_no": "BN-2", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertEqual(follow_up["items"][0]["ca_version"], 2)
        self.assertEqual(follow_up["items"][0]["net_quantity"], 1500)

    def test_concurrent_confirm_allows_single_winner(self):
        self.service.create(TRADER, "TRD-1", instruction(quantity=1000))
        view = self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        self.service.create(TRADER, "TRD-2", instruction(quantity=500, price=13.0))
        batch_id = view["batch"]["id"]
        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def confirm(actor):
            barrier.wait(timeout=10)
            try:
                self.service.confirm_batch(actor, batch_id, 1)
                with lock:
                    outcomes.append("ok")
            except Conflict as exc:
                with lock:
                    outcomes.append(exc)

        threads = [threading.Thread(target=confirm, args=(actor,)) for actor in (SUP_A, SUP_B)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, Conflict)]
        self.assertEqual(len(conflicts), 1)
        details = conflicts[0].details
        self.assertEqual(details["batch_no"], "BN-1")
        self.assertEqual(details["batch_state"], "confirmed")
        self.assertEqual([item["reference"] for item in details["unbatched"]], ["TRD-2"])
        self.assertEqual(self.service.get_batch(OFFICER, batch_id)["batch"]["state"], "confirmed")

    def test_failed_write_leaves_no_half_batch_and_retry_continues(self):
        repository = CrashOnceRepository(str(Path(self.temp.name) / "crash.db"))
        service = Service(repository, DomainRules(), AuditRecorder(repository))
        service.create(TRADER, "TRD-1", instruction(quantity=1000))
        service.create(TRADER, "TRD-2", instruction(side="sell", quantity=400, price=12.6, ca="none", ratio=1.0))
        with self.assertRaises(RuntimeError):
            service.generate_batch(OFFICER, {"batch_no": "BN-9", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        # 不留半张批次：批次头与明细整体回滚，指令全部释放
        self.assertIsNone(repository.get_batch_by_no("BN-9"))
        self.assertEqual(repository.locked_record_ids(), set())
        self.assertEqual(len(service.unbatched_instructions(OFFICER, "ACC-1", "CNY", 2)), 2)
        # 同一批次号重提：继续核对并完整落库
        view = service.generate_batch(OFFICER, {"batch_no": "BN-9", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertFalse(view["replayed"])
        self.assertTrue(view["verification"]["ok"])
        self.assertEqual(len(view["items"]), 2)
        # 再次重提同一批次号：幂等返回原批次并附核对结果
        replay = service.generate_batch(OFFICER, {"batch_no": "BN-9", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["batch"]["id"], view["batch"]["id"])
        self.assertTrue(replay["verification"]["ok"])
        self.assertEqual(len(replay["items"]), 2)
        # 批次号被其他键值占用则冲突
        with self.assertRaises(Conflict):
            service.generate_batch(OFFICER, {"batch_no": "BN-9", "account": "ACC-2", "currency": "CNY", "settlement_day": 2})

    def test_legacy_upgrade_backfills_original_parameters(self):
        db_path = str(Path(self.temp.name) / "legacy.db")
        repository = Repository(db_path)
        legacy = {
            "instrument": "OLDCO",
            "side": "buy",
            "quantity": 800,
            "price": 9.5,
            "fees": 5.0,
            "currency": "CNY",
            "settlement_day": 1,
            "corporate_action": "split",
            "action_ratio": 1.5,
            "account": "ACC-9",
            "gross_amount": 7600.0,
            "net_amount": 7605.0,
            "adjusted_quantity": 800,
            "adjusted_price": 9.5,
        }
        repository.create("LEG-1", "captured", legacy, "legacy-loader")
        service = Service(repository, DomainRules(), AuditRecorder(repository))
        result = service.migrate_legacy()
        self.assertEqual(result["backfilled"], ["OLDCO"])
        versions = repository.list_ca_versions("OLDCO")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], 1)
        self.assertEqual(versions[0]["action_type"], "split")
        self.assertEqual(versions[0]["ratio"], 1.5)
        self.assertEqual(versions[0]["created_by"], "system-migration")
        # 升级幂等：重复执行不再补登
        self.assertEqual(service.migrate_legacy()["backfilled"], [])
        # 旧指令入批时采用补登的版本1
        view = service.generate_batch(OFFICER, {"batch_no": "BN-L1", "account": "ACC-9", "currency": "CNY", "settlement_day": 1})
        self.assertEqual(view["items"][0]["ca_version"], 1)
        self.assertEqual(view["items"][0]["net_quantity"], 1200)

    def test_batched_instruction_cannot_be_settled_individually(self):
        record = self.service.create(TRADER, "TRD-1", instruction(quantity=1000))
        self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        record = self.service.act(CA_TEAM, record["id"], record["version"], "apply_corporate", {})
        record = self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, record["id"], record["version"], "settle", {"delivered_quantity": 2000, "cash_paid": 12518.0})

    def test_cancel_releases_instructions_for_rebatch(self):
        self.service.create(TRADER, "TRD-1", instruction(quantity=1000))
        view = self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        cancelled = self.service.cancel_batch(SUP_A, view["batch"]["id"], 1, "比例修正，重新净额")
        self.assertEqual(cancelled["batch"]["state"], "cancelled")
        self.assertEqual(self.service.repository.locked_record_ids(), set())
        self.service.register_corporate_action(CA_TEAM, {"instrument": "ACME", "action_type": "split", "ratio": 3.0})
        follow_up = self.service.generate_batch(OFFICER, {"batch_no": "BN-2", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertEqual(follow_up["items"][0]["ca_version"], 2)
        self.assertEqual(follow_up["items"][0]["net_quantity"], 3000)

    def test_dividend_entitlement_flows_into_net_cash(self):
        self.service.create(TRADER, "TRD-1", instruction(ca="dividend", ratio=0.5, quantity=1000, price=10.0, fees=0.0))
        view = self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        self.assertEqual(view["items"][0]["net_quantity"], 1000)
        self.assertEqual(view["items"][0]["net_amount"], -9500.0)
        self.assertEqual(view["batch"]["net_cash"], -9500.0)

    def test_permissions_and_empty_batch(self):
        self.service.create(TRADER, "TRD-1", instruction(quantity=1000))
        with self.assertRaises(PermissionDenied):
            self.service.generate_batch(TRADER, dict(BATCH_REQ))
        with self.assertRaises(PermissionDenied):
            self.service.register_corporate_action(TRADER, {"instrument": "ACME", "action_type": "split", "ratio": 3.0})
        view = self.service.generate_batch(OFFICER, dict(BATCH_REQ))
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch(OFFICER, view["batch"]["id"], 1)
        with self.assertRaises(ValidationError):
            self.service.generate_batch(OFFICER, {"batch_no": "BN-X", "account": "NOPE", "currency": "CNY", "settlement_day": 2})
