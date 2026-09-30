import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.rules import DomainRules
from src.repository import Repository


OFFICER = Actor("officer-1", "settlement_officer")
OFFICER_2 = Actor("officer-2", "settlement_officer")
CA_USER = Actor("ca-1", "corporate_actions")


def base_data(**overrides):
    data = {
        'instrument': 'ACME', 'side': 'buy', 'quantity': 1000, 'price': 12.5, 'fees': 18.0,
        'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'split',
        'action_ratio': 2.0, 'account': 'ACC-1',
    }
    data.update(overrides)
    return data


class NetBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)
        self._price_seq = 1000

    def tearDown(self):
        self.temp.cleanup()

    def create_approved(self, ref, side='buy', account='ACC-1', ratio=2.0, ca='split', price=None):
        if price is None:
            self._price_seq += 7
            price = 10.0 + self._price_seq / 100.0
        record = self.service.create(Actor("trader-1", "trader"), ref, base_data(side=side, account=account, action_ratio=ratio, corporate_action=ca, price=price))
        if ca != 'none':
            record = self.service.act(CA_USER, record["id"], record["version"], "apply_corporate", {})
        record = self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        return record

    def test_batch_totals_and_freeze_keeps_old_ratio(self):
        r1 = self.create_approved("TRD-3001", side='buy')
        r2 = self.create_approved("TRD-3002", side='sell')
        batch = self.service.generate_net_batch(OFFICER, {"batch_no": "NB-001", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertEqual(batch["status"], "open")
        self.assertEqual(batch["totals"]["instruction_count"], 2)
        self.assertEqual(batch["totals"]["net_quantity"], 0)  # +2000 / -2000

        # 交收前公司行动比例修正为 3.0，已生成的批次不受影响
        revision = self.service.publish_corporate(CA_USER, {"instrument": "ACME", "corporate_action": "split", "action_ratio": 3.0, "remark": "比例修正"})
        self.assertEqual(revision["version"], 2)

        confirmed = self.service.confirm_net_batch(OFFICER, "NB-001")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertFalse(confirmed["replayed"])
        self.assertEqual(confirmed["totals"]["net_quantity"], 0)
        for item in confirmed["items"]:
            self.assertEqual(item["frozen"], 1)
            self.assertEqual(item["action_version"], 1)
            self.assertEqual(item["action_ratio"], 2.0)
            self.assertEqual(item["settlement_quantity"], 2000)

        settled_1 = self.service.get_record(OFFICER, r1["id"])
        settled_2 = self.service.get_record(OFFICER, r2["id"])
        self.assertEqual(settled_1["state"], "settled")
        self.assertEqual(settled_1["payload"]["frozen_action_version"], 1)
        self.assertEqual(settled_1["payload"]["frozen_action_ratio"], 2.0)
        self.assertEqual(settled_1["payload"]["frozen_quantity"], 2000)
        self.assertEqual(settled_1["payload"]["net_batch_no"], "NB-001")
        self.assertEqual(settled_2["payload"]["frozen_quantity"], -2000)
        # 新版本没有污染已冻结指令
        self.assertEqual(settled_1["payload"]["adjusted_quantity"], 2000)

    def test_leftover_instructions_recomputed_with_new_version(self):
        self.create_approved("TRD-3101", side='buy')
        self.create_approved("TRD-3102", side='buy')
        batch_a = self.service.generate_net_batch(OFFICER, {"batch_no": "NB-A", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertEqual(batch_a["totals"]["instruction_count"], 2)

        # 又有两笔指令复核入池（仍按 v1=2.0）
        self.create_approved("TRD-3103", side='buy')
        self.create_approved("TRD-3104", side='sell')
        # 主管二生成包含全部 4 笔的批次
        batch_b = self.service.generate_net_batch(OFFICER_2, {"batch_no": "NB-B", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.assertEqual(batch_b["totals"]["instruction_count"], 4)

        # 交收前比例修正
        self.service.publish_corporate(CA_USER, {"instrument": "ACME", "corporate_action": "split", "action_ratio": 3.0})

        confirmed = self.service.confirm_net_batch(OFFICER, "NB-A")
        # 未入批的 3103/3104 按新版本 3.0 重算：1000 * 3.0 = 3000
        leftovers = {item["reference"]: item for item in confirmed["leftovers"]}
        self.assertEqual(set(leftovers), {"TRD-3103", "TRD-3104"})
        self.assertTrue(all(item["action_version"] == 2 and item["settlement_quantity"] == 3000 for item in leftovers.values()))
        r3 = self.service.get_record(OFFICER, 3)
        self.assertEqual(r3["payload"]["action_version"], 2)
        self.assertEqual(r3["payload"]["adjusted_quantity"], 3000)
        self.assertEqual(r3["state"], "approved")  # 重算但仍未交收

        # 另一位主管确认 NB-B：3101/3102 已被冻结（冲突），3103/3104 已重算（快照过期）
        try:
            self.service.confirm_net_batch(OFFICER_2, "NB-B")
            self.fail("应当抛出冲突")
        except Conflict as exc:
            details = exc.details
            collisions = {c["record_id"] for c in details["collisions"]}
            stale = {s["record_id"] for s in details["stale"]}
            self.assertEqual(collisions, {1, 2})
            self.assertEqual(stale, {3, 4})  # 3/4 已被赢家按新版本重算，败方快照过期
            # 败方同时拿到按新版本重算的未入批指令清单（赢家批次之外仍可组批的指令）
            leftover_ids = {item["record_id"] for item in details["leftovers"]}
            self.assertEqual(leftover_ids, {3, 4})
            self.assertTrue(all(item["action_version"] == 2 for item in details["leftovers"]))

        # NB-B 仍是 open，没有留下半张批次
        still_open = self.service.get_batch(OFFICER_2, "NB-B")
        self.assertEqual(still_open["status"], "open")
        self.assertTrue(all(item["frozen"] == 0 for item in still_open["items"]))
        # 记录 3/4 没有被错误交收
        self.assertEqual(self.service.get_record(OFFICER, 3)["state"], "approved")

    def test_concurrent_confirmation_only_one_wins(self):
        self.create_approved("TRD-3201", side='buy')
        self.create_approved("TRD-3202", side='sell')
        self.service.generate_net_batch(OFFICER, {"batch_no": "NB-C1", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        self.service.generate_net_batch(OFFICER_2, {"batch_no": "NB-C2", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})

        outcomes = {}

        def confirm(officer, batch_no):
            try:
                return ("ok", self.service.confirm_net_batch(officer, batch_no))
            except Conflict as exc:
                return ("conflict", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(confirm, OFFICER, "NB-C1")
            f2 = pool.submit(confirm, OFFICER_2, "NB-C2")
            outcomes["C1"] = f1.result()
            outcomes["C2"] = f2.result()

        statuses = sorted((batch_no, result[0]) for batch_no, result in outcomes.items())
        winners = [batch_no for batch_no, status in statuses if status == "ok"]
        losers = [batch_no for batch_no, status in statuses if status == "conflict"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        loser_error = outcomes[losers[0]][1]
        self.assertEqual(len(loser_error.details["collisions"]), 2)
        self.assertIn("leftovers", loser_error.details)

        # 记录只交收一次
        records = self.service.list_records(OFFICER, state="settled")
        self.assertEqual(len(records), 2)

    def test_write_failure_leaves_no_half_batch_and_resume(self):
        self.create_approved("TRD-3301", side='buy')
        self.service.generate_net_batch(OFFICER, {"batch_no": "NB-RETRY", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})

        # 在仓储连接工厂上注入一次提交失败（模拟写入失败），验证事务回滚不留半张批次
        repository = self.service.repository
        real_connect = repository._connect
        state = {"fail_remaining": 1}

        class CommitFailConnection:
            def __init__(self, inner):
                self._inner = inner

            def commit(self):
                if state["fail_remaining"] > 0:
                    state["fail_remaining"] -= 1
                    raise sqlite3.OperationalError("simulated disk failure")
                self._inner.commit()

            def rollback(self):
                self._inner.rollback()

            def close(self):
                self._inner.close()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        def failing_connect():
            return CommitFailConnection(real_connect())

        repository._connect = failing_connect
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.confirm_net_batch(OFFICER, "NB-RETRY")
        finally:
            repository._connect = real_connect

        # 失败后批次仍 open、无冻结、指令仍 approved：没有半张批次
        batch = self.service.get_batch(OFFICER, "NB-RETRY")
        self.assertEqual(batch["status"], "open")
        self.assertTrue(all(item["frozen"] == 0 for item in batch["items"]))
        self.assertEqual(self.service.get_record(OFFICER, 1)["state"], "approved")

        # 同一批次号重提，继续核对并成功
        resumed = self.service.confirm_net_batch(OFFICER, "NB-RETRY")
        self.assertEqual(resumed["status"], "confirmed")
        self.assertFalse(resumed["replayed"])
        self.assertEqual(self.service.get_record(OFFICER, 1)["state"], "settled")

        # 再次重提为幂等返回
        again = self.service.confirm_net_batch(OFFICER, "NB-RETRY")
        self.assertTrue(again["replayed"])
        self.assertEqual(again["status"], "confirmed")

    def test_reconfirm_after_regeneration_uses_new_batch(self):
        self.create_approved("TRD-3401", side='buy')
        self.service.generate_net_batch(OFFICER, {"batch_no": "NB-OLD", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        record = self.service.get_record(OFFICER, 1)
        # 指令在确认前被 fail，open 批次快照失效
        failed = self.service.act(OFFICER, 1, record["version"], "fail", {"fail_reason": "对手方问题"})
        self.assertEqual(failed["state"], "failed")
        with self.assertRaises(Conflict) as ctx:
            self.service.confirm_net_batch(OFFICER, "NB-OLD")
        self.assertEqual(ctx.exception.details["stale"][0]["state"], "failed")

    def test_create_with_stale_ratio_is_rejected(self):
        # v1=2.0 自动登记后发布 v2=3.0，再用旧比例建单必须拒绝
        self.service.create(Actor("trader-1", "trader"), "TRD-3501", base_data(action_ratio=2.0))
        self.service.publish_corporate(CA_USER, {"instrument": "ACME", "corporate_action": "split", "action_ratio": 3.0})
        with self.assertRaises(Conflict) as ctx:
            self.service.create(Actor("trader-1", "trader"), "TRD-3502", base_data(action_ratio=2.0))
        self.assertEqual(ctx.exception.details["latest_version"], 2)
        # 用新比例可以正常建单并锁定 v2
        record = self.service.create(Actor("trader-1", "trader"), "TRD-3503", base_data(action_ratio=3.0))
        self.assertEqual(record["payload"]["action_version"], 2)
        self.assertEqual(record["payload"]["adjusted_quantity"], 3000)

    def test_empty_group_rejected_and_batch_no_unique(self):
        with self.assertRaises(ValidationError):
            self.service.generate_net_batch(OFFICER, {"batch_no": "NB-X", "account": "ACC-1", "currency": "CNY", "settlement_day": 9})
        self.create_approved("TRD-3601", side='buy')
        self.service.generate_net_batch(OFFICER, {"batch_no": "NB-DUP", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        with self.assertRaises(Conflict):
            self.service.generate_net_batch(OFFICER_2, {"batch_no": "NB-DUP", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})

    def test_permissions(self):
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.generate_net_batch(Actor("t", "trader"), {"batch_no": "NB-P", "account": "ACC-1", "currency": "CNY", "settlement_day": 2})
        with self.assertRaises(PermissionDenied):
            self.service.confirm_net_batch(Actor("t", "trader"), "NB-P")
        with self.assertRaises(PermissionDenied):
            self.service.publish_corporate(OFFICER, {"instrument": "ACME", "corporate_action": "split", "action_ratio": 3.0})


class RuleSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_snapshot_signed_and_dividend_cash(self):
        buy = {"id": 1, "reference": "B1", "version": 1, "payload": {
            "side": "buy", "net_amount": 12518.0, "quantity": 1000, "adjusted_quantity": 2000,
            "effective_quantity": 2000, "action_ratio": 2.0, "action_version": 1}}
        snap = self.rules.net_snapshot(buy)
        self.assertEqual(snap["signed_quantity"], 2000)
        self.assertEqual(snap["net_cash_amount"], 12518.0)

        div = dict(buy)
        div["payload"] = dict(buy["payload"], corporate_action="dividend", cash_entitlement=500.0, effective_quantity=1000)
        snap_div = self.rules.net_snapshot(div)
        self.assertEqual(snap_div["net_cash_amount"], 12018.0)  # 买方现金支出扣减股利

        sell = {"id": 2, "reference": "S1", "version": 1, "payload": {
            "side": "sell", "net_amount": 12482.0, "quantity": 1000, "adjusted_quantity": 2000,
            "effective_quantity": 2000, "action_ratio": 2.0, "action_version": 1}}
        totals = self.rules.batch_totals([snap, self.rules.net_snapshot(sell)])
        self.assertEqual(totals["net_quantity"], 0)
        self.assertEqual(totals["net_direction"], "flat")

    def test_recompute_payload(self):
        prepared = self.rules.prepare_create(base_data(action_ratio=2.0))
        prepared["corporate_applied"] = True
        prepared["effective_quantity"] = prepared["adjusted_quantity"]
        payload = self.rules.recompute_payload(prepared, {"version": 2, "action_ratio": 3.0})
        self.assertEqual(payload["action_version"], 2)
        self.assertEqual(payload["adjusted_quantity"], 3000)
        self.assertEqual(payload["effective_quantity"], 3000)


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "legacy.db")

    def tearDown(self):
        self.temp.cleanup()

    def _build_legacy_db(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                payload TEXT NOT NULL,
                created_by TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        # 旧数据：无 account、无 action_version，比例 2.0
        payload_old = ('{"action_ratio": 2.0, "adjusted_price": 6.25, "adjusted_quantity": 2000, '
                       '"corporate_action": "split", "currency": "CNY", "fees": 18.0, "gross_amount": 12500.0, '
                       '"instrument": "ACME", "net_amount": 12518.0, "price": 12.5, "quantity": 1000, '
                       '"settlement_day": 2, "side": "buy"}')
        payload_none = '{"corporate_action": "none", "currency": "USD", "instrument": "CASH", "quantity": 10, "price": 1.0, "fees": 0, "net_amount": 10.0, "gross_amount": 10.0, "adjusted_quantity": 10, "adjusted_price": 1.0, "settlement_day": 1, "side": "buy", "action_ratio": 1.0}'
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,1,?,?,?,'2026-01-01','2026-01-01')",
            ("OLD-1", "captured", payload_old, "legacy", "legacy"),
        )
        connection.execute(
            "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,1,?,?,?,'2026-01-01','2026-01-01')",
            ("OLD-2", "captured", payload_none, "legacy", "legacy"),
        )
        connection.commit()
        connection.close()

    def test_legacy_data_gets_version_with_original_ratio(self):
        self._build_legacy_db()
        repository = Repository(self.db_path)
        record = repository.get(1)
        self.assertEqual(record["payload"]["account"], "DEFAULT")
        self.assertEqual(record["payload"]["action_version"], 1)
        self.assertEqual(record["payload"]["adjusted_quantity"], 2000)
        versions = repository.list_corporate_versions("ACME", "split")
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]["version"], 1)
        self.assertEqual(versions[0]["action_ratio"], 2.0)  # 按原参数补一版
        self.assertEqual(versions[0]["published_by"], "migration")
        none_record = repository.get(2)
        self.assertEqual(none_record["payload"]["action_version"], 0)

        # 迁移幂等：再次初始化不产生新版本/重复审计
        repository = Repository(self.db_path)
        self.assertEqual(len(repository.list_corporate_versions("ACME", "split")), 1)
        timeline = repository.audit_timeline(1)
        self.assertEqual(len([e for e in timeline if e["action"] == "schema_migrated"]), 1)

        # 迁移后的旧指令可正常进入净额流程
        svc = build_service(self.db_path)
        r1 = svc.act(Actor("ca", "corporate_actions"), 1, 1, "apply_corporate", {})
        r1 = svc.act(OFFICER, 1, r1["version"], "approve", {})
        batch = svc.generate_net_batch(OFFICER, {"batch_no": "NB-LEGACY", "account": "DEFAULT", "currency": "CNY", "settlement_day": 2})
        self.assertEqual(batch["items"][0]["action_version"], 1)
        self.assertEqual(batch["items"][0]["settlement_quantity"], 2000)
