import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError


def base_payload(station="ST-50"):
    return {
        "frequency_mhz": 2400.0,
        "bandwidth_mhz": 20.0,
        "station_id": station,
        "region": "north",
        "strength_dbm": -35,
        "detected_at": "2026-09-27T10:00:00+00:00",
        "reporter": "monitor-1",
    }


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _prepared_item(self):
        item = self.service.create_item(base_payload(), "analyst-1", "analyst", request_id="REQ-CREATE")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"],
                                request_id="REQ-ASSESS")
        self.service.add_source(item["id"], {
            "source_type": "fixed_monitor",
            "external_id": "OBS-1",
            "observed_at": "2026-09-27T10:05:00+00:00",
            "strength_dbm": -35,
            "bandwidth_mhz": 20.0,
            "frequency_mhz": 2400.0,
        }, "analyst-1", "analyst", request_id="REQ-SOURCE")
        item = self.service.get_item(item["id"])
        self.service.act(item["id"], "locate", {"location": "cell-1", "confidence": 0.8},
                         "field-1", "field_operator", item["version"], request_id="REQ-LOC-SETUP")
        return self.service.get_item(item["id"])

    def test_duplicate_request_id_applied_once_and_replayed(self):
        item = self._prepared_item()
        suspend_payload = {"authorization_code": "REG-NORTH-1"}
        first = self.service.act(item["id"], "suspend", dict(suspend_payload),
                                 "coord-1", "coordinator", item["version"], "north",
                                 request_id="REQ-LOC")
        self.assertFalse(first.get("idempotent_replay"))
        self.assertEqual(first["version"], item["version"] + 1)

        # 同一请求编号重复到达：只入账一次，返回同一条结果
        replay = self.service.act(item["id"], "suspend", dict(suspend_payload),
                                  "coord-1", "coordinator", item["version"], "north",
                                  request_id="REQ-LOC")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["version"], first["version"])
        fresh = self.service.get_item(item["id"])
        self.assertEqual(fresh["version"], first["version"])
        actions = [e for e in fresh["audit"] if e["event_type"] == "suspend"]
        self.assertEqual(len(actions), 1)

        # 同编号不同内容拒绝
        with self.assertRaises(ConflictError) as exc:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-9"},
                             "coord-1", "coordinator", item["version"], "north",
                             request_id="REQ-LOC")
        self.assertEqual(exc.exception.code, "idempotency_key_conflict")

    def test_source_request_id_recorded_once(self):
        item = self.service.create_item(base_payload(), "analyst-1", "analyst", request_id="REQ-SC")
        payload = {
            "source_type": "fixed_monitor",
            "external_id": "OBS-DUP",
            "observed_at": "2026-09-27T10:05:00+00:00",
            "strength_dbm": -35,
            "bandwidth_mhz": 20.0,
        }
        first = self.service.add_source(item["id"], dict(payload), "analyst-1", "analyst",
                                        request_id="REQ-SRC-DUP")
        replay = self.service.add_source(item["id"], dict(payload), "analyst-1", "analyst",
                                         request_id="REQ-SRC-DUP")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["source_id"], first["source_id"])
        self.assertEqual(len(self.service.get_item(item["id"])["sources"]), 1)

    def test_two_regions_concurrent_first_writer_wins(self):
        item = self._prepared_item()
        version = item["version"]
        barrier = threading.Barrier(2)
        outcomes = {}

        def submit(tag, code):
            barrier.wait()
            try:
                result = self.service.act(item["id"], "suspend", {"authorization_code": code},
                                          "coord-" + tag, "coordinator", version, "north",
                                          request_id="REQ-SUS-" + tag)
                outcomes[tag] = ("ok", result["version"], result["status"])
            except ConflictError as exc:
                outcomes[tag] = ("conflict", exc.code, exc.details)

        t1 = threading.Thread(target=submit, args=("A", "REG-NORTH-1"))
        t2 = threading.Thread(target=submit, args=("B", "REG-NORTH-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        statuses = [v[0] for v in outcomes.values()]
        self.assertEqual(sorted(statuses), ["conflict", "ok"])
        winner = next(v for v in outcomes.values() if v[0] == "ok")
        loser = next(v for v in outcomes.values() if v[0] == "conflict")
        winner_code = "REG-NORTH-1" if outcomes["A"] is winner else "REG-NORTH-2"
        details = loser[2]
        self.assertEqual(loser[1], "version_conflict")
        self.assertEqual(details["current_version"], version + 1)
        self.assertEqual(details["current_status"], "suspended")
        self.assertIn("authorization_code", details["conflicting_fields"])
        self.assertEqual(details["conflicting_fields"]["authorization_code"]["current"], winner_code)

        # 后到请求拿最新版本重做：先读到最新状态，再沿状态机继续推进协调
        self.assertEqual(self.service.get_item(item["id"])["status"], "suspended")
        redone = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-8"},
                                  "coord-B", "coordinator", version + 1, "north",
                                  request_id="REQ-COORD-RETRY")
        self.assertEqual(redone["status"], "coordinating")
        self.assertEqual(redone["version"], version + 2)

    def test_write_failure_retried_with_same_id_is_applied_once(self):
        item = self._prepared_item()
        real_apply = self.repo.apply_action
        calls = {"n": 0}

        def flaky_apply(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("simulated disk write failure")
            return real_apply(*args, **kwargs)

        self.repo.apply_action = flaky_apply
        with self.assertRaises(sqlite3.OperationalError):
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-7"},
                             "coord-1", "coordinator", item["version"], "north",
                             request_id="REQ-RETRY")
        self.repo.apply_action = real_apply

        # 写失败后用同一编号重试：业务效果只累计一次
        done = self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-7"},
                                "coord-1", "coordinator", item["version"], "north",
                                request_id="REQ-RETRY")
        self.assertEqual(done["version"], item["version"] + 1)
        suspended = [e for e in self.service.get_item(item["id"])["audit"] if e["event_type"] == "suspend"]
        self.assertEqual(len(suspended), 1)

    def test_restart_resumes_from_breakpoint_without_double_effect(self):
        item = self._prepared_item()
        # 模拟崩溃残留：台账停在 processing，业务未写入
        conn = self.repo.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO request_log(request_id,fingerprint,scope,item_id,status,created_at) "
                "VALUES(?,?,?,?,?,?)",
                ("REQ-STALE", "fp", "item.action", item["id"], "processing", "2026-09-27T10:00:00+00:00"),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

        # 重启：新 Repository 初始化时把残留断点交还给重试
        repo2 = Repository(self.tmp.name)
        repo2.initialize()
        service2 = Service(repo2)
        done = service2.act(
            item["id"], "suspend", {"authorization_code": "REG-NORTH-8"},
            "coord-1", "coordinator", item["version"], "north", request_id="REQ-STALE",
        )
        self.assertEqual(done["version"], item["version"] + 1)
        again = service2.act(
            item["id"], "suspend", {"authorization_code": "REG-NORTH-8"},
            "coord-1", "coordinator", item["version"], "north", request_id="REQ-STALE",
        )
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["version"], item["version"] + 1)
        suspended = [e for e in service2.get_item(item["id"])["audit"] if e["event_type"] == "suspend"]
        self.assertEqual(len(suspended), 1)


if __name__ == "__main__":
    unittest.main()
