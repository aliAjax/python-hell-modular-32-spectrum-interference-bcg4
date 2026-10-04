import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError
from . import rules


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    request_id TEXT PRIMARY KEY,
                    item_id INTEGER,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _item_from_conn(self, conn, item_id):
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("item_not_found", "业务实体不存在")
        return self._row_to_item(row)

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    # ---- idempotency (请求编号入账一次，失败按编号重试，重启从断点继续) ----

    def find_idempotency(self, request_id):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT result FROM idempotency WHERE request_id=? AND status='completed'",
                (request_id,),
            ).fetchone()
            if row and row["result"]:
                return json.loads(row["result"])
            return None
        finally:
            conn.close()

    def _idempotency_begin(self, conn, request_id, item_id, operation):
        conn.execute(
            "INSERT INTO idempotency(request_id,item_id,operation,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, item_id, operation, "pending", None, now_iso(), now_iso()),
        )

    def _idempotency_complete(self, conn, request_id, result):
        conn.execute(
            "UPDATE idempotency SET status='completed', result=?, updated_at=? WHERE request_id=?",
            (canonical_json(result), now_iso(), request_id),
        )

    def _idempotency_retry_or_begin(self, conn, request_id, item_id, operation):
        """返回 (stored_result_or_None, should_proceed)。completed 直接返回入账结果；pending 视为断点续做。"""
        existing = conn.execute(
            "SELECT status, result FROM idempotency WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if existing is None:
            self._idempotency_begin(conn, request_id, item_id, operation)
            return None, True
        if existing["status"] == "completed":
            stored = json.loads(existing["result"]) if existing["result"] else None
            return stored, False
        conn.execute("DELETE FROM idempotency WHERE request_id=?", (request_id,))
        self._idempotency_begin(conn, request_id, item_id, operation)
        return None, True

    # ---- 业务写入 ----

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role, request_id=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if request_id:
                stored, proceed = self._idempotency_retry_or_begin(conn, request_id, None, "create_item")
                if not proceed:
                    conn.execute("ROLLBACK")
                    return stored
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            # 初始上报即第一份来源记录，事件出生即有依据
            seed_external_id = "INIT-%d" % item_id
            seed_payload = {
                "source_type": "initial_report",
                "external_id": seed_external_id,
                "observed_at": payload["detected_at"],
                "strength_dbm": payload["strength_dbm"],
                "bandwidth_mhz": payload.get("bandwidth_mhz"),
                "region": payload.get("region"),
                "station_id": payload.get("station_id"),
                "frequency_mhz": payload.get("frequency_mhz"),
            }
            conn.execute(
                "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, "initial_report", seed_external_id, canonical_json(seed_payload), payload["detected_at"], now_iso()),
            )
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            basis = rules.make_basis(source_id, payload["detected_at"], payload["strength_dbm"], payload.get("bandwidth_mhz"))
            payload["current_basis"] = basis
            payload.setdefault("basis_history", [])
            conn.execute(
                "UPDATE items SET payload=?, updated_at=? WHERE id=?",
                (canonical_json(payload), now_iso(), item_id),
            )
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key, "basis_source_id": source_id})
            self.append_audit(conn, item_id, "source_recorded", actor, role, {
                "source_id": source_id, "source_type": "initial_report", "external_id": seed_external_id,
            })
            result = self._item_from_conn(conn, item_id)
            if request_id:
                conn.execute("UPDATE idempotency SET item_id=? WHERE request_id=?", (item_id, request_id))
                self._idempotency_complete(conn, request_id, result)
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, source_payload, observed_at, actor, role, request_id=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if request_id:
                stored, proceed = self._idempotency_retry_or_begin(conn, request_id, item_id, "add_source")
                if not proceed:
                    conn.execute("ROLLBACK")
                    return stored
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            item = self._row_to_item(row)
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(source_payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            current_payload = item["payload"]
            new_basis = rules.make_basis(
                source_id, observed_at, source_payload["strength_dbm"], source_payload.get("bandwidth_mhz")
            )
            old_basis = current_payload.get("current_basis")
            basis_changed = False
            if old_basis is None or _parse_iso(observed_at) > _parse_iso(old_basis["observed_at"]):
                basis_changed = True
                current_payload.setdefault("basis_history", []).append({
                    "source_id": source_id,
                    "observed_at": observed_at,
                    "strength_dbm": source_payload["strength_dbm"],
                    "bandwidth_mhz": source_payload.get("bandwidth_mhz"),
                    "reason": "newer_observation",
                    "recorded_at": now_iso(),
                })
                current_payload["current_basis"] = new_basis
            else:
                current_payload.setdefault("basis_history", []).append({
                    "source_id": source_id,
                    "observed_at": observed_at,
                    "strength_dbm": source_payload["strength_dbm"],
                    "bandwidth_mhz": source_payload.get("bandwidth_mhz"),
                    "reason": "late_arrival",
                    "note": "观测时间不晚于当前依据，仅入历史",
                    "recorded_at": now_iso(),
                })
            new_status = None
            impact_events = []
            if basis_changed:
                current_payload, new_status, impact_events = rules.apply_basis_impact(
                    current_payload, new_basis, old_basis, item["status"]
                )
            new_version = int(item["version"]) + 1
            if new_status is not None:
                conn.execute(
                    "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                    (new_status, new_version, canonical_json(current_payload), now_iso(), item_id),
                )
            else:
                conn.execute(
                    "UPDATE items SET version=?,payload=?,updated_at=? WHERE id=?",
                    (new_version, canonical_json(current_payload), now_iso(), item_id),
                )
            if basis_changed:
                self.append_audit(conn, item_id, "basis_updated", actor, role, {
                    "new_basis_source_id": source_id,
                    "old_basis_source_id": (old_basis or {}).get("source_id"),
                    "impact": impact_events,
                })
            result = {
                "id": source_id,
                "item_id": item_id,
                "source_type": source_type,
                "external_id": external_id,
                "payload": source_payload,
                "observed_at": observed_at,
                "basis": new_basis,
                "basis_changed": basis_changed,
            }
            if request_id:
                self._idempotency_complete(conn, request_id, result)
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def _conflict_details(self, row):
        payload = json.loads(row["payload"])
        conflicts = []
        for field in ("coordination_agreement", "resolution", "suspend_authorization", "assessment", "location", "cancellation"):
            if payload.get(field) is not None:
                conflicts.append({"field": field, "value": payload[field]})
        return {
            "latest_version": int(row["version"]),
            "current_status": row["status"],
            "conflicts": conflicts,
        }

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None, request_id=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if request_id:
                stored, proceed = self._idempotency_retry_or_begin(conn, request_id, item_id, action)
                if not proceed:
                    conn.execute("ROLLBACK")
                    return stored
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError(
                    "version_conflict",
                    "记录已被其他辖区更新，请按最新版本和冲突项重做",
                    self._conflict_details(row),
                )
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            result = self._item_from_conn(conn, item_id)
            if request_id:
                self._idempotency_complete(conn, request_id, result)
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
