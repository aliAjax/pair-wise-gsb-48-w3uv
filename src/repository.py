"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BatchNoExists(Exception):
    """同一批次号已落库：供重提幂等处理，不属于错误。"""

    def __init__(self, batch_no: str) -> None:
        super().__init__("批次号已存在: %s" % batch_no)
        self.batch_no = batch_no


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
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
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ca_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action_type TEXT NOT NULL,
                    ratio REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(instrument, version)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_day INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    net_cash REAL NOT NULL,
                    totals TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL UNIQUE REFERENCES records(id),
                    instrument TEXT NOT NULL,
                    side TEXT NOT NULL,
                    ca_version INTEGER,
                    net_quantity INTEGER NOT NULL,
                    net_amount REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(account, currency, settlement_day);
                CREATE INDEX IF NOT EXISTS idx_batch_items_batch ON batch_items(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batch_events_batch ON batch_events(batch_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["totals"] = json.loads(item["totals"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 公司行动版本 ----

    def insert_ca_version(self, instrument: str, action_type: str, ratio: float, note: str, actor_id: str) -> Dict[str, Any]:
        """登记新版本；参数与最新版本一致时幂等返回，不产生重复版本。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute("SELECT * FROM ca_versions WHERE instrument=? ORDER BY version DESC LIMIT 1", (instrument,)).fetchone()
            if latest is not None and latest["action_type"] == action_type and abs(float(latest["ratio"]) - float(ratio)) < 1e-9:
                connection.rollback()
                return dict(latest)
            version = int(latest["version"]) + 1 if latest is not None else 1
            connection.execute(
                "INSERT INTO ca_versions(instrument,version,action_type,ratio,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (instrument, version, action_type, float(ratio), note, actor_id, now),
            )
            row = connection.execute("SELECT * FROM ca_versions WHERE instrument=? AND version=?", (instrument, version)).fetchone()
            connection.commit()
        return dict(row)

    def latest_ca_version(self, instrument: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ca_versions WHERE instrument=? ORDER BY version DESC LIMIT 1", (instrument,)).fetchone()
        return dict(row) if row is not None else None

    def list_ca_versions(self, instrument: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if instrument:
                rows = connection.execute("SELECT * FROM ca_versions WHERE instrument=? ORDER BY version", (instrument,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM ca_versions ORDER BY instrument, version").fetchall()
        return [dict(row) for row in rows]

    def backfill_legacy_ca_versions(self, actor_id: str = "system-migration") -> List[str]:
        """旧数据升级：按指令原始公司行动参数为每个标的补登版本1，幂等可重入。"""
        now = _now()
        backfilled: List[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT payload FROM records ORDER BY id").fetchall()
            originals: Dict[str, Any] = {}
            for row in rows:
                payload = json.loads(row["payload"])
                instrument = payload.get("instrument")
                action = payload.get("corporate_action")
                if instrument and action and action != "none" and instrument not in originals:
                    originals[instrument] = (action, float(payload.get("action_ratio", 1.0)))
            for instrument, (action, ratio) in originals.items():
                exists = connection.execute("SELECT 1 FROM ca_versions WHERE instrument=? LIMIT 1", (instrument,)).fetchone()
                if exists is None:
                    connection.execute(
                        "INSERT INTO ca_versions(instrument,version,action_type,ratio,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (instrument, 1, action, ratio, "旧数据升级按原参数补登", actor_id, now),
                    )
                    backfilled.append(instrument)
            connection.commit()
        return backfilled

    # ---- 净额批次 ----

    def create_batch(self, batch_no: str, account: str, currency: str, settlement_day: int, items: List[Dict[str, Any]], totals: Dict[str, int], net_cash: float, actor_id: str) -> Dict[str, Any]:
        """批次与全部明细单事务写入：要么整批落库，要么整体回滚，不留半张批次。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute("SELECT id FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
            if duplicate is not None:
                connection.rollback()
                raise BatchNoExists(batch_no)
            record_ids = [int(item["record_id"]) for item in items]
            locked = connection.execute(
                "SELECT record_id FROM batch_items WHERE record_id IN (%s)" % ",".join("?" for _ in record_ids),
                record_ids,
            ).fetchall()
            if locked:
                connection.rollback()
                raise Conflict("部分指令已入其他批次", details={"locked_record_ids": [int(row["record_id"]) for row in locked]})
            cursor = connection.execute(
                "INSERT INTO batches(batch_no,account,currency,settlement_day,state,version,net_cash,totals,item_count,created_by,confirmed_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_no, account, currency, settlement_day, "open", 1, net_cash, json.dumps(totals, ensure_ascii=False, sort_keys=True), len(items), actor_id, "", now, now),
            )
            batch_id = int(cursor.lastrowid)
            self._insert_batch_items(connection, batch_id, items, now)
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "generated", actor_id, 1, json.dumps({"batch_no": batch_no, "item_count": len(items)}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch_by_no(batch_no)

    def _insert_batch_items(self, connection: sqlite3.Connection, batch_id: int, items: List[Dict[str, Any]], now: str) -> None:
        for item in items:
            connection.execute(
                "INSERT INTO batch_items(batch_id,record_id,instrument,side,ca_version,net_quantity,net_amount,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, int(item["record_id"]), item["instrument"], item["side"], item["ca_version"], int(item["net_quantity"]), float(item["net_amount"]), now),
            )

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def get_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        return self._batch_row(row) if row is not None else None

    def list_batches(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM batches WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def locked_record_ids(self) -> Set[int]:
        """已入批（未取消）指令集合：取消批次会删除明细并释放指令。"""
        with self._connect() as connection:
            rows = connection.execute("SELECT record_id FROM batch_items").fetchall()
        return {int(row["record_id"]) for row in rows}

    def confirm_batch(self, batch_id: int, expected_version: int, actor_id: str) -> Dict[str, Any]:
        """乐观锁确认：版本不符即冲突，同一时刻只有一位主管能确认成功。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            if row["state"] != "open":
                connection.rollback()
                raise Conflict("批次已%s，不能重复确认" % row["state"])
            version = int(expected_version) + 1
            connection.execute("UPDATE batches SET state='confirmed',version=?,confirmed_by=?,updated_at=? WHERE id=?", (version, actor_id, now, batch_id))
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "confirmed", actor_id, version, json.dumps({"batch_no": row["batch_no"], "item_count": int(row["item_count"])}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch(batch_id)

    def cancel_batch(self, batch_id: int, expected_version: int, actor_id: str, reason: str) -> Dict[str, Any]:
        """取消未确认批次：删除明细、释放指令，事件里留存快照。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            if row["state"] != "open":
                connection.rollback()
                raise Conflict("只有未确认的批次可以取消")
            items = connection.execute("SELECT record_id FROM batch_items WHERE batch_id=?", (batch_id,)).fetchall()
            released = [int(item["record_id"]) for item in items]
            connection.execute("DELETE FROM batch_items WHERE batch_id=?", (batch_id,))
            version = int(expected_version) + 1
            connection.execute("UPDATE batches SET state='cancelled',version=?,updated_at=? WHERE id=?", (version, now, batch_id))
            connection.execute(
                "INSERT INTO batch_events(batch_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (batch_id, "cancelled", actor_id, version, json.dumps({"reason": reason, "released_record_ids": released}, ensure_ascii=False, sort_keys=True), now),
            )
            connection.commit()
        return self.get_batch(batch_id)
