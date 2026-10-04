import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


REPLAY_MARKER = "__idempotent_replay__"


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
                    request_id TEXT,
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
                CREATE TABLE IF NOT EXISTS request_log (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    item_id INTEGER,
                    status TEXT NOT NULL,
                    result_payload TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )
            self._recover_pending(conn)
        finally:
            conn.close()

    def _recover_pending(self, conn):
        """服务重启后的断点恢复：台账与业务写入在同一事务中提交，
        正常情况下不会残留 processing；残留一律回滚为未入账，等待按编号重试。"""
        conn.execute(
            "UPDATE request_log SET status='abandoned', completed_at=? WHERE status='processing'",
            (now_iso(),),
        )

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

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

    def _load_request(self, conn, request_id):
        row = conn.execute("SELECT * FROM request_log WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["result_payload"] = json.loads(result["result_payload"]) if result["result_payload"] else None
        return result

    def _claim_request(self, conn, request_id, fingerprint, scope, item_id):
        """在已持有的写事务内处理幂等台账。返回已存在记录（重放/冲突），或占位 None。"""
        existing = self._load_request(conn, request_id)
        if existing is not None:
            if existing["status"] in ("processing", "abandoned"):
                # 残留断点：本次重试接管该编号
                conn.execute(
                    "UPDATE request_log SET fingerprint=?, scope=?, item_id=?, status='processing', "
                    "result_payload=NULL, completed_at=NULL WHERE request_id=?",
                    (fingerprint, scope, item_id, request_id),
                )
                return None
            if existing["fingerprint"] != fingerprint:
                raise ConflictError(
                    "idempotency_key_conflict",
                    "同一请求编号提交了不同内容，请更换请求编号",
                    {"request_id": request_id, "stored_scope": existing["scope"]},
                )
            return existing
        try:
            conn.execute(
                "INSERT INTO request_log(request_id,fingerprint,scope,item_id,status,result_payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (request_id, fingerprint, scope, item_id, "processing", None, now_iso()),
            )
        except sqlite3.IntegrityError:
            existing = self._load_request(conn, request_id)
            if existing is not None and existing["fingerprint"] == fingerprint:
                return existing
            raise ConflictError("idempotency_key_conflict", "同一请求编号提交了不同内容，请更换请求编号")
        return None

    def _complete_request(self, conn, request_id, result):
        conn.execute(
            "UPDATE request_log SET status='completed', result_payload=?, completed_at=? WHERE request_id=?",
            (canonical_json(result), now_iso(), request_id),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role,
                    request_id=None, fingerprint=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replayed = None
            if request_id is not None:
                replayed = self._claim_request(conn, request_id, fingerprint, "item.create", None)
            if replayed is not None:
                result = dict(replayed["result_payload"])
                conn.execute("COMMIT")
                return {REPLAY_MARKER: True, "result": result}
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
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            if request_id is not None:
                self._complete_request(
                    conn, request_id, {"kind": "item", "item_id": item_id, "version": 1}
                )
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def lookup_request(self, request_id):
        conn = self.connect()
        try:
            row = self._load_request(conn, request_id)
            return row
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

    def get_source(self, source_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise NotFoundError("source_not_found", "来源记录不存在")
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role,
                   promotion=None, request_id=None, fingerprint=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replayed = None
            if request_id is not None:
                replayed = self._claim_request(conn, request_id, fingerprint, "item.source", item_id)
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if replayed is not None:
                conn.execute("COMMIT")
                return {REPLAY_MARKER: True, "result": replayed["result_payload"]}
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
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
                {"source_id": source_id, "source_type": source_type, "external_id": external_id,
                 "observed_at": observed_at},
            )

            promoted = bool(promotion and promotion.get("promoted"))
            new_version = None
            if promoted:
                new_payload = promotion["new_payload"]
                new_basis = new_payload["basis"]
                new_basis["source_id"] = source_id
                row = conn.execute("SELECT version FROM items WHERE id=?", (item_id,)).fetchone()
                new_version = int(row["version"]) + 1
                conn.execute(
                    "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                    (promotion["new_status"], new_version, canonical_json(new_payload), now_iso(), item_id),
                )
                self.append_audit(
                    conn,
                    item_id,
                    "basis_promoted",
                    actor,
                    role,
                    {
                        "source_id": source_id,
                        "basis": new_basis,
                        "reason": promotion["reason"],
                        "changed_fields": promotion["changed_fields"],
                        "reopening": new_payload.get("reopening"),
                    },
                )
            result = {
                "kind": "source",
                "item_id": item_id,
                "source_id": source_id,
                "promoted": promoted,
                "version": new_version,
            }
            if request_id is not None:
                self._complete_request(conn, request_id, result)
            conn.execute("COMMIT")
            return {"source_id": source_id, "item_id": item_id, "promoted": promoted,
                    "promotion": promotion, "version": new_version}
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

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload,
                     expected_version=None, request_id=None, fingerprint=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            replayed = None
            if request_id is not None:
                replayed = self._claim_request(conn, request_id, fingerprint, "item.action", item_id)
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if replayed is not None:
                conn.execute("COMMIT")
                return {REPLAY_MARKER: True, "result": replayed["result_payload"]}
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,request_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), request_id, now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            if request_id is not None:
                self._complete_request(
                    conn,
                    request_id,
                    {"kind": "item", "item_id": item_id, "version": version, "action": action},
                )
            conn.execute("COMMIT")
            return self.get_item(item_id)
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
