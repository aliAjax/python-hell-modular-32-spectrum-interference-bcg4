import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def base_payload(**over):
    p = {
        "frequency_mhz": 2400.0,
        "bandwidth_mhz": 20.0,
        "station_id": "ST-01",
        "region": "north",
        "strength_dbm": -45,
        "detected_at": "2026-09-27T10:00:00+00:00",
        "reporter": "monitor-1",
    }
    p.update(over)
    return p


class EvidenceChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _flow(self, item_id, version, through="resolve"):
        item = self.service.act(item_id, "assess", {}, "analyst-1", "analyst", version)
        item = self.service.act(item_id, "locate", {"location": "cell-7", "confidence": 0.9},
                                "field-1", "field_operator", item["version"])
        item = self.service.act(item_id, "suspend", {"authorization_code": "REG-NORTH-1"},
                                "coord-1", "coordinator", item["version"], "north")
        if through == "suspend":
            return item
        item = self.service.act(item_id, "coordinate", {"coordination_agreement": "AGC-7"},
                                 "coord-1", "coordinator", item["version"], "north")
        if through == "coordinate":
            return item
        item = self.service.act(item_id, "resolve", {"measurement_cleared": True, "evidence": "scan-7"},
                                "coord-1", "coordinator", item["version"], "north")
        return item

    def test_new_item_confirmed_by_initial_report(self):
        created = self.service.create_item(base_payload(), "analyst-1", "analyst")
        item = self.service.get_item(created["id"])
        self.assertTrue(item["basis_confirmed"])
        self.assertIsNotNone(item["basis"])
        self.assertEqual(item["basis"]["source_id"], item["sources"][-1]["id"])

    def test_basis_takes_later_observation_late_arrival_history_only(self):
        created = self.service.create_item(base_payload(), "analyst-1", "analyst")
        item = self.service.get_item(created["id"])
        r1 = self.service.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-1",
            "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -40,
            "bandwidth_mhz": 20.0, "region": "north",
        }, "analyst-1", "analyst", "north")
        self.assertTrue(r1["basis_changed"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["basis"]["source_id"], r1["id"])

        r2 = self.service.add_source(item["id"], {
            "source_type": "fixed_station", "external_id": "FS-1",
            "observed_at": "2026-09-27T09:00:00+00:00", "strength_dbm": -99,
            "region": "north",
        }, "analyst-1", "analyst", "north")
        self.assertFalse(r2["basis_changed"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["basis"]["source_id"], r1["id"])
        reasons = [h["reason"] for h in item["payload"]["basis_history"]]
        self.assertIn("newer_observation", reasons)
        self.assertIn("late_arrival", reasons)

    def test_basis_change_after_coordination_voids_unexecuted(self):
        created = self.service.create_item(base_payload(), "analyst-1", "analyst")
        item = self._flow(created["id"], created["version"], through="coordinate")
        self.assertEqual(item["status"], "coordinating")
        auth = item["payload"]["suspend_authorizations"][0]
        self.assertEqual(auth["status"], "issued")
        self.assertEqual(auth["basis_source_id"], item["payload"]["current_basis"]["source_id"])

        r = self.service.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-LATE",
            "observed_at": "2026-09-27T14:00:00+00:00", "strength_dbm": -60,
            "bandwidth_mhz": 20.0, "region": "north",
        }, "analyst-1", "analyst", "north")
        self.assertTrue(r["basis_changed"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "located")
        auth = item["payload"]["suspend_authorizations"][0]
        self.assertEqual(auth["status"], "voided")
        self.assertIsNotNone(auth["basis_source_id"])
        self.assertIn("coordination_agreement_voided", item["payload"])

    def test_resolved_case_keeps_basis(self):
        created = self.service.create_item(base_payload(), "analyst-1", "analyst")
        item = self._flow(created["id"], created["version"], through="resolve")
        self.assertEqual(item["status"], "resolved")
        original_basis = item["payload"]["current_basis"]["source_id"]
        auth = item["payload"]["suspend_authorizations"][0]
        self.assertEqual(auth["status"], "issued")

        self.service.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-CLOSED",
            "observed_at": "2026-09-27T15:00:00+00:00", "strength_dbm": -70,
            "bandwidth_mhz": 20.0, "region": "north",
        }, "analyst-1", "analyst", "north")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["payload"]["suspend_authorizations"][0]["status"], "issued")
        self.assertEqual(item["payload"]["resolution"]["basis_source_id"], original_basis)

    def test_optimistic_conflict_returns_latest_version_and_conflicts(self):
        created = self.service.create_item(base_payload(), "analyst-1", "analyst")
        item = self._flow(created["id"], created["version"], through="coordinate")
        stale_version = item["version"] - 1
        with self.assertRaises(ConflictError) as ctx:
            self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-2"},
                             "coord-2", "coordinator", stale_version, "north")
        self.assertEqual(ctx.exception.code, "version_conflict")
        self.assertEqual(ctx.exception.details["latest_version"], item["version"])
        fields = [c["field"] for c in ctx.exception.details["conflicts"]]
        self.assertIn("coordination_agreement", fields)

    def test_same_request_number_posts_once(self):
        item = self.service.create_item(base_payload(), "analyst-1", "analyst", request_id="req-create-1")
        item2 = self.service.create_item(base_payload(), "analyst-1", "analyst", request_id="req-create-1")
        self.assertEqual(item2["id"], item["id"])
        self.assertEqual(item2["version"], item["version"])

        r1 = self.service.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-1",
            "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -40, "region": "north",
        }, "analyst-1", "analyst", "north", request_id="req-src-1")
        r2 = self.service.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-1",
            "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -40, "region": "north",
        }, "analyst-1", "analyst", "north", request_id="req-src-1")
        self.assertEqual(r2["id"], r1["id"])
        item = self.service.get_item(item["id"])
        self.assertEqual(len(item["sources"]), 2)

    def test_retry_after_restart_continues_without_accumulation(self):
        item = self.service.create_item(base_payload(), "analyst-1", "analyst")
        self.service.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-1",
            "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -40, "region": "north",
        }, "analyst-1", "analyst", "north", request_id="req-restart-1")
        # simulate service restart: new service, same database
        service2 = Service(Repository(self.tmp.name))
        before = service2.get_item(item["id"])
        self.assertEqual(len(before["sources"]), 2)
        r = service2.add_source(item["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-1",
            "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -40, "region": "north",
        }, "analyst-1", "analyst", "north", request_id="req-restart-1")
        self.assertEqual(r["id"], before["sources"][0]["id"])
        after = service2.get_item(item["id"])
        self.assertEqual(len(after["sources"]), 2)

    def test_legacy_item_without_sources_unconfirmed(self):
        # insert a legacy item directly (no sources) to simulate pre-upgrade data
        conn = self.repo.connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("spectrum_interference", "legacy|1", "located", 1, json.dumps({
                "frequency_mhz": 2400.0, "bandwidth_mhz": 20.0, "station_id": "ST-LEG",
                "region": "north", "strength_dbm": -45, "detected_at": "2026-09-27T10:00:00+00:00",
                "reporter": "monitor-1",
            }), "analyst-1", "analyst", "2026-09-27T10:00:00+00:00", "2026-09-27T10:00:00+00:00"),
        )
        conn.execute("COMMIT")
        conn.close()
        legacy = self.repo.list_items()[0]
        self.assertIsNone(legacy["payload"].get("current_basis"))
        with self.assertRaises(DomainError) as ctx:
            self.service.act(legacy["id"], "suspend", {"authorization_code": "REG-X"},
                             "coord-1", "coordinator", 1, "north")
        self.assertEqual(ctx.exception.code, "basis_unconfirmed")
        # submitting a source confirms it
        self.service.add_source(legacy["id"], {
            "source_type": "mobile_monitor", "external_id": "MM-1",
            "observed_at": "2026-09-27T12:00:00+00:00", "strength_dbm": -40, "region": "north",
        }, "analyst-1", "analyst", "north")
        legacy = self.service.get_item(legacy["id"])
        self.assertTrue(legacy["basis_confirmed"])


if __name__ == "__main__":
    unittest.main()
