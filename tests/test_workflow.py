import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_complete_interference_workflow(self):
        item = self.service.create_item({
            "frequency_mhz": 2400.0,
            "bandwidth_mhz": 20.0,
            "station_id": "ST-01",
            "region": "north",
            "strength_dbm": -35,
            "detected_at": "2026-09-27T10:00:00+00:00",
            "reporter": "monitor-1",
        }, "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["payload"]["assessment"]["level"], "critical")
        self.service.add_source(item["id"], {
            "source_type": "fixed_monitor",
            "external_id": "OBS-001",
            "observed_at": "2026-09-27T10:05:00+00:00",
            "strength_dbm": -35,
            "bandwidth_mhz": 20.0,
            "frequency_mhz": 2400.0,
        }, "analyst-1", "analyst")
        item = self.service.get_item(item["id"])
        self.assertTrue(item["basis_confirmed"])
        item = self.service.act(item["id"], "locate", {"location": "cell-7", "confidence": 0.9}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-1"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "coordinate", {"coordination_agreement": "AGC-7"}, "coord-1", "coordinator", item["version"], "north")
        item = self.service.act(item["id"], "resolve", {"measurement_cleared": True, "evidence": "scan-7"}, "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        self.assertGreaterEqual(len(item["audit"]), 7)
        self.assertEqual(item["payload"]["resolution"]["basis"]["source_type"], "fixed_monitor")


if __name__ == "__main__":
    unittest.main()
