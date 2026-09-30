"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, DEFAULT_ACCOUNT, NotFound


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


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

    @contextmanager
    def transaction(self):
        """立即加写锁的事务上下文，异常即回滚，保证不留下半张批次。"""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def _init_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = {
                name
                for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            fresh = "records" not in existing
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
                CREATE TABLE IF NOT EXISTS corporate_action_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    corporate_action TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action_ratio REAL NOT NULL,
                    remark TEXT NOT NULL DEFAULT '',
                    published_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(instrument, corporate_action, version)
                );
                CREATE TABLE IF NOT EXISTS net_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_day INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    totals TEXT NOT NULL DEFAULT '{}',
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS net_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES net_batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    record_version INTEGER NOT NULL,
                    action_version INTEGER NOT NULL,
                    action_ratio REAL NOT NULL,
                    settlement_quantity INTEGER NOT NULL,
                    signed_quantity INTEGER NOT NULL,
                    net_cash_amount REAL NOT NULL,
                    frozen INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, record_id)
                );
                CREATE TABLE IF NOT EXISTS batch_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES net_batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_ca_lookup ON corporate_action_versions(instrument, corporate_action, version);
                CREATE INDEX IF NOT EXISTS idx_batch_group ON net_batches(account, currency, settlement_day);
                CREATE INDEX IF NOT EXISTS idx_batch_items_record ON net_batch_items(record_id);
                -- 同一条指令最多被一个已确认批次冻结
                CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_items_frozen
                    ON net_batch_items(record_id) WHERE frozen = 1;
                """
            )
            if not fresh:
                self._migrate_legacy_records(connection)
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate_legacy_records(self, connection: sqlite3.Connection) -> None:
        """旧数据升级：按指令原参数补一版公司行动版本并回填版本/账户字段（幂等）。"""
        now = _now()
        rows = connection.execute("SELECT id, payload FROM records").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            changed = False
            if not payload.get("account"):
                payload["account"] = DEFAULT_ACCOUNT
                changed = True

            corporate = payload.get("corporate_action", "none")
            if corporate != "none" and "action_version" not in payload:
                ratio = float(payload.get("action_ratio", 0.0))
                versions = connection.execute(
                    "SELECT version, action_ratio FROM corporate_action_versions WHERE instrument=? AND corporate_action=? ORDER BY version",
                    (payload.get("instrument"), corporate),
                ).fetchall()
                matched = next((v for v in versions if round(float(v["action_ratio"]), 6) == round(ratio, 6)), None)
                if matched is not None:
                    payload["action_version"] = int(matched["version"])
                else:
                    new_version = (int(versions[-1]["version"]) if versions else 0) + 1
                    connection.execute(
                        "INSERT INTO corporate_action_versions(instrument, corporate_action, version, action_ratio, remark, published_by, created_at) VALUES(?,?,?,?,?,?,?)",
                        (payload.get("instrument"), corporate, new_version, ratio, "旧数据升级按原参数补版", "migration", now),
                    )
                    payload["action_version"] = new_version
                changed = True
            elif corporate == "none" and "action_version" not in payload:
                payload["action_version"] = 0
                changed = True

            if changed:
                connection.execute("UPDATE records SET payload=? WHERE id=?", (_dumps(payload), row["id"]))
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "schema_migrated", "migration", 1, _dumps({"action_version": payload.get("action_version"), "account": payload.get("account")}), now),
                )

    # ---- 指令 ---------------------------------------------------------

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dumps(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dumps({"state": state}), now),
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
                (state, version, _dumps(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dumps(details), now),
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
                (record_id, action, actor_id, int(row["version"]), _dumps(details), _now()),
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

    # ---- 公司行动版本 -------------------------------------------------

    def get_latest_corporate(self, instrument: str, corporate_action: str, connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        sql = (
            "SELECT * FROM corporate_action_versions WHERE instrument=? AND corporate_action=? "
            "ORDER BY version DESC LIMIT 1"
        )
        params = (instrument, corporate_action)
        if connection is not None:
            row = connection.execute(sql, params).fetchone()
        else:
            with self._connect() as own:
                row = own.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def list_corporate_versions(self, instrument: str = None, corporate_action: str = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM corporate_action_versions"
        clauses = []
        params: List[Any] = []
        if instrument:
            clauses.append("instrument=?")
            params.append(instrument)
        if corporate_action:
            clauses.append("corporate_action=?")
            params.append(corporate_action)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY instrument, corporate_action, version DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def publish_corporate(self, instrument: str, corporate_action: str, ratio: float, remark: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = self.get_latest_corporate(instrument, corporate_action, connection)
            if latest is not None and round(float(latest["action_ratio"]), 6) == round(float(ratio), 6):
                connection.rollback()
                raise Conflict("公司行动比例与当前最新版本相同", {"version": int(latest["version"])})
            version = (int(latest["version"]) if latest is not None else 0) + 1
            connection.execute(
                "INSERT INTO corporate_action_versions(instrument, corporate_action, version, action_ratio, remark, published_by, created_at) VALUES(?,?,?,?,?,?,?)",
                (instrument, corporate_action, version, ratio, remark, actor_id, now),
            )
            row = connection.execute(
                "SELECT * FROM corporate_action_versions WHERE instrument=? AND corporate_action=? AND version=?",
                (instrument, corporate_action, version),
            ).fetchone()
            connection.commit()
        return dict(row)

    # ---- 净额批次 -----------------------------------------------------

    def get_batch(self, batch_no: str, connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM net_batches WHERE batch_no=?"
        if connection is not None:
            row = connection.execute(sql, (batch_no,)).fetchone()
        else:
            with self._connect() as own:
                row = own.execute(sql, (batch_no,)).fetchone()
        if row is None:
            return None
        batch = dict(row)
        batch["totals"] = json.loads(batch["totals"] or "{}")
        return batch

    def require_batch(self, batch_no: str, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        batch = self.get_batch(batch_no, connection)
        if batch is None:
            raise NotFound("净额批次不存在")
        return batch

    def list_batch_items(self, batch_id: int, connection: sqlite3.Connection = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM net_batch_items WHERE batch_id=? ORDER BY id"
        if connection is not None:
            rows = connection.execute(sql, (batch_id,)).fetchall()
        else:
            with self._connect() as own:
                rows = own.execute(sql, (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_batch_audit(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batch_audit_events WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def eligible_approved_records(self, account: str, currency: str, settlement_day: int, connection: sqlite3.Connection = None, exclude_batch_id: int = None) -> List[Dict[str, Any]]:
        """该分组下已复核、尚未被任何已确认批次冻结的指令。

        exclude_batch_id 给定时，同时排除该批次内的快照（用于计算“未入批指令”）。
        open 批次不排除，留待确认时竞争冻结。
        """
        sql = (
            "SELECT r.* FROM records r "
            "WHERE r.state='approved' "
            "AND json_extract(r.payload,'$.account')=? "
            "AND json_extract(r.payload,'$.currency')=? "
            "AND json_extract(r.payload,'$.settlement_day')=? "
            "AND NOT EXISTS (SELECT 1 FROM net_batch_items i WHERE i.record_id=r.id AND i.frozen=1) "
        )
        params: List[Any] = [account, currency, int(settlement_day)]
        if exclude_batch_id is not None:
            sql += "AND NOT EXISTS (SELECT 1 FROM net_batch_items bi WHERE bi.record_id=r.id AND bi.batch_id=?) "
            params.append(int(exclude_batch_id))
        sql += "ORDER BY r.id"
        if connection is not None:
            rows = connection.execute(sql, params).fetchall()
        else:
            with self._connect() as own:
                rows = own.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def create_net_batch(self, batch_no: str, account: str, currency: str, settlement_day: int, totals: Dict[str, Any], snapshots: List[Dict[str, Any]], actor_id: str, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        now = _now()
        own = None
        if connection is None:
            own = self._connect()
            own.execute("BEGIN IMMEDIATE")
            connection = own
        try:
            existing = self.get_batch(batch_no, connection)
            if existing is not None:
                raise Conflict("批次号已存在", {"batch_no": batch_no})
            cursor = connection.execute(
                "INSERT INTO net_batches(batch_no,account,currency,settlement_day,status,totals,confirmed_by,created_by,created_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (batch_no, account, currency, int(settlement_day), "open", _dumps(totals), "", actor_id, now, None),
            )
            batch_id = int(cursor.lastrowid)
            for snap in snapshots:
                connection.execute(
                    "INSERT INTO net_batch_items(batch_id,record_id,record_version,action_version,action_ratio,settlement_quantity,signed_quantity,net_cash_amount,frozen,created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
                    (
                        batch_id,
                        snap["record_id"],
                        snap["record_version"],
                        snap["action_version"],
                        snap["action_ratio"],
                        snap["settlement_quantity"],
                        snap["signed_quantity"],
                        snap["net_cash_amount"],
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO batch_audit_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
                (batch_id, "batch_generated", actor_id, _dumps({"totals": totals, "instruction_count": len(snapshots)}), now),
            )
        except sqlite3.IntegrityError as exc:
            if own is not None:
                own.rollback()
                own.close()
            raise Conflict("批次号已存在", {"batch_no": batch_no}) from exc
        except Exception:
            if own is not None:
                own.rollback()
                own.close()
            raise
        if own is not None:
            own.commit()
            own.close()
        if connection is not None and own is None:
            batch = self.require_batch(batch_no, connection)
            result = dict(batch)
            result["items"] = self.list_batch_items(int(batch["id"]), connection)
            result["audit"] = []
            return result
        return self.get_batch_detail(batch_no)

    def get_batch_detail(self, batch_no: str) -> Dict[str, Any]:
        batch = self.require_batch(batch_no)
        items = self.list_batch_items(int(batch["id"]))
        audit = self.list_batch_audit(int(batch["id"]))
        result = dict(batch)
        result["items"] = items
        result["audit"] = audit
        return result

    def list_batches(self, status: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM net_batches WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM net_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            batch = dict(row)
            batch["totals"] = json.loads(batch["totals"] or "{}")
            result.append(batch)
        return result

    # ---- 事务内使用的细粒度操作（配合 transaction() 上下文） ----------

    def insert_record(self, connection: sqlite3.Connection, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            cursor = connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference, state, 1, _dumps(payload), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        record_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, "created", actor_id, 1, _dumps({"state": state}), now),
        )
        return self._row(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

    def ensure_first_corporate_version(self, connection: sqlite3.Connection, instrument: str, corporate_action: str, ratio: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        connection.execute(
            "INSERT INTO corporate_action_versions(instrument, corporate_action, version, action_ratio, remark, published_by, created_at) VALUES(?,?,?,?,?,?,?)",
            (instrument, corporate_action, 1, float(ratio), "创建指令时自动登记首版", actor_id, now),
        )
        row = connection.execute(
            "SELECT * FROM corporate_action_versions WHERE instrument=? AND corporate_action=? AND version=1",
            (instrument, corporate_action),
        ).fetchone()
        return dict(row)

    def get_record_for_update(self, connection: sqlite3.Connection, record_id: int) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def update_record_tx(
        self,
        connection: sqlite3.Connection,
        record_id: int,
        expected_version: int,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        action: str,
        details: Dict[str, Any],
    ) -> Dict[str, Any]:
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, _dumps(payload), actor_id, _now(), record_id),
        )
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, _dumps(details), _now()),
        )
        return self._row(connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone())

    def frozen_item_of_other_batch(self, connection: sqlite3.Connection, record_id: int, batch_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT i.*, b.batch_no FROM net_batch_items i JOIN net_batches b ON b.id=i.batch_id "
            "WHERE i.record_id=? AND i.frozen=1 AND i.batch_id<>? LIMIT 1",
            (record_id, batch_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_item_frozen(self, connection: sqlite3.Connection, item_id: int) -> None:
        connection.execute("UPDATE net_batch_items SET frozen=1 WHERE id=?", (item_id,))

    def confirm_batch_row(self, connection: sqlite3.Connection, batch_id: int, totals: Dict[str, Any], actor_id: str) -> None:
        connection.execute(
            "UPDATE net_batches SET status='confirmed', totals=?, confirmed_by=?, confirmed_at=? WHERE id=?",
            (_dumps(totals), actor_id, _now(), batch_id),
        )

    def add_batch_audit(self, connection: sqlite3.Connection, batch_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO batch_audit_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
            (batch_id, action, actor_id, _dumps(details), _now()),
        )
