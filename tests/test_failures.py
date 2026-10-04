import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)
        self.payload = {
            "frequency_mhz": 5800.0,
            "bandwidth_mhz": 10.0,
            "station_id": "ST-02",
            "region": "west",
            "strength_dbm": -55,
            "detected_at": "2026-09-27T11:00:00+00:00",
            "reporter": "monitor-2",
        }

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_duplicate_permission_and_region(self):
        item = self.service.create_item(self.payload, "m", "monitor")
        with self.assertRaises(ConflictError):
            self.service.create_item(self.payload, "m", "monitor")
        item = self.service.act(item["id"], "assess", {}, "m", "monitor", item["version"])
        self.service.add_source(item["id"], {
            "source_type": "fixed_monitor",
            "external_id": "OBS-100",
            "observed_at": "2026-09-27T11:05:00+00:00",
            "strength_dbm": -55,
            "bandwidth_mhz": 10.0,
            "frequency_mhz": 5800.0,
        }, "m", "monitor")
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.8}, "f", "field_operator", item["version"])
        with self.assertRaises(DomainError) as forbidden:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-X"}, "f", "field_operator", item["version"], "west")
        self.assertEqual(forbidden.exception.status, 403)
        with self.assertRaises(DomainError) as mismatch:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-X"}, "c", "coordinator", item["version"], "east")
        self.assertEqual(mismatch.exception.code, "region_mismatch")

    def test_measurement_revision_preserves_original_and_version_conflict(self):
        item = self.service.create_item(self.payload, "m", "monitor")
        revised = self.service.act(item["id"], "correct_measurement", {"strength_dbm": -72, "reason": "sensor drift"}, "m", "monitor", item["version"])
        self.assertEqual(revised["payload"]["measurement_revisions"][0]["old_strength_dbm"], -55.0)
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "assess", {}, "m", "monitor", item["version"])


if __name__ == "__main__":
    unittest.main()
