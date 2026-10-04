import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def make_item(service, region="north", frequency=2400.0, bandwidth=20.0, strength=-35):
    return service.create_item({
        "frequency_mhz": frequency,
        "bandwidth_mhz": bandwidth,
        "station_id": "ST-01",
        "region": region,
        "strength_dbm": strength,
        "detected_at": "2026-09-27T10:00:00+00:00",
        "reporter": "monitor-1",
    }, "analyst-1", "analyst")


def add_source(service, item_id, external_id, observed_at, strength, bandwidth=20.0, frequency=2400.0):
    return service.add_source(item_id, {
        "source_type": "fixed_monitor",
        "external_id": external_id,
        "observed_at": observed_at,
        "strength_dbm": strength,
        "bandwidth_mhz": bandwidth,
        "frequency_mhz": frequency,
    }, "analyst-1", "analyst")


def bring_to_coordinating(service, item_id, auth="REG-NORTH-1", region="north"):
    item = service.get_item(item_id)
    item = service.act(item_id, "assess", {}, "analyst-1", "analyst", item["version"])
    add_source(service, item_id, "OBS-1", "2026-09-27T10:05:00+00:00", -35)
    item = service.get_item(item_id)
    item = service.act(item_id, "locate", {"location": "cell-7", "confidence": 0.9},
                       "field-1", "field_operator", item["version"], region)
    item = service.act(item_id, "suspend", {"authorization_code": auth},
                       "coord-1", "coordinator", item["version"], region)
    item = service.act(item_id, "coordinate", {"coordination_agreement": "AGC-7"},
                       "coord-1", "coordinator", item["version"], region)
    return service.get_item(item_id)


class CredentialChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_latest_observation_is_current_basis_older_goes_history(self):
        item = make_item(self.service)
        add_source(self.service, item["id"], "OBS-EARLY", "2026-09-27T10:05:00+00:00", -40)
        add_source(self.service, item["id"], "OBS-LATE", "2026-09-27T10:20:00+00:00", -60, bandwidth=10.0)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["payload"]["basis"]["external_id"], "OBS-LATE")
        self.assertEqual(item["payload"]["basis"]["source_id"], 2)
        self.assertEqual(item["payload"]["strength_dbm"], -60.0)
        self.assertEqual(item["payload"]["bandwidth_mhz"], 10.0)
        self.assertEqual(item["payload"]["basis_history"][0]["external_id"], "OBS-EARLY")
        self.assertEqual(item["payload"]["assessment"]["level"], "critical")

        # 更晚的当前依据已存在，晚到的只进历史，参数和版本不变
        before_version = item["version"]
        result = add_source(self.service, item["id"], "OBS-LAGGARD", "2026-09-27T10:10:00+00:00", -20)
        self.assertFalse(result["promoted"])
        self.assertEqual(result["promotion"]["reason"], "late_source_history_only")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["version"], before_version)
        self.assertEqual(item["payload"]["basis"]["external_id"], "OBS-LATE")
        self.assertEqual(item["payload"]["strength_dbm"], -60.0)
        chain = self.service.credential_chain(item["id"])
        roles = {o["source_id"]: o["role"] for o in chain["observations"]}
        self.assertEqual(roles[2], "current_basis")
        self.assertEqual(roles[3], "history")

    def test_legacy_item_without_sources_is_unconfirmed_and_explains_failure(self):
        item = make_item(self.service)
        self.assertFalse(self.service.get_item(item["id"])["basis_confirmed"])
        item = self.service.act(item["id"], "assess", {}, "analyst-1", "analyst", item["version"])
        with self.assertRaises(DomainError) as exc:
            self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.9},
                             "f", "field_operator", item["version"])
        self.assertEqual(exc.exception.code, "basis_unconfirmed")
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(exc.exception.details["reason"], "no_confirmed_source")
        chain = self.service.credential_chain(item["id"])
        self.assertFalse(chain["basis_confirmed"])
        self.assertIn("旧事件", chain["invalidation"]["message"])

        # 补录来源后可提交
        add_source(self.service, item["id"], "OBS-BACKFILL", "2026-09-27T09:00:00+00:00", -35)
        item = self.service.get_item(item["id"])
        self.assertTrue(item["basis_confirmed"])
        self.service.act(item["id"], "locate", {"location": "x", "confidence": 0.9},
                         "f", "field_operator", item["version"])

    def test_issued_authorization_voided_after_coordination_when_basis_changes(self):
        item = make_item(self.service)
        item = bring_to_coordinating(self.service, item["id"])

        # 协调后补签一张停用授权（未执行）
        item = self.service.act(item["id"], "authorize_suspend", {"authorization_code": "REG-NORTH-2"},
                                "coord-2", "coordinator", item["version"], "north")
        self.assertEqual(item["payload"]["authorization"]["status"], "issued")
        self.assertEqual(item["payload"]["authorization"]["basis"]["external_id"], "OBS-1")

        # 更晚来源改动强度和带宽：未执行授权作废，待结案回退
        result = add_source(self.service, item["id"], "OBS-2", "2026-09-27T11:00:00+00:00",
                            -72, bandwidth=5.0)
        self.assertTrue(result["promoted"])
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "reopened")
        self.assertIsNone(item["payload"]["authorization"])
        self.assertIsNone(item["payload"]["coordination"])
        voided = item["payload"]["voided_authorizations"][-1]
        self.assertEqual(voided["authorization_code"], "REG-NORTH-2")
        self.assertEqual(voided["status"], "voided")
        self.assertEqual(voided["void_reason"], "basis_changed_after_coordination")
        self.assertIn("strength_dbm", voided["changed_fields"])
        self.assertEqual(item["payload"]["suspensions"][0]["status"], "executed")
        self.assertEqual(item["payload"]["suspensions"][0]["basis"]["external_id"], "OBS-1")
        self.assertEqual(item["payload"]["coordination_history"][0]["status"], "superseded")
        self.assertEqual(item["payload"]["basis"]["external_id"], "OBS-2")

        # 重新定位后可基于新依据重做处置，旧授权编号不能再执行
        item = self.service.act(item["id"], "locate", {"location": "cell-9", "confidence": 0.95},
                                "field-1", "field_operator", item["version"], "north")
        with self.assertRaises(DomainError) as exc:
            self.service.act(item["id"], "suspend", {"authorization_code": "REG-NORTH-2"},
                             "coord-2", "coordinator", item["version"], "north")
        self.assertEqual(exc.exception.code, "invalid_authorization")

    def test_executed_suspension_keeps_original_basis_and_resolution_resets(self):
        item = make_item(self.service)
        item = bring_to_coordinating(self.service, item["id"])
        add_source(self.service, item["id"], "OBS-2", "2026-09-27T11:00:00+00:00", -72)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "reopened")
        executed = item["payload"]["suspensions"][0]
        self.assertEqual(executed["status"], "executed")
        self.assertEqual(executed["basis"]["external_id"], "OBS-1")
        self.assertNotIn("resolution", item["payload"])

    def test_only_strength_or_bandwidth_change_triggers_voiding(self):
        item = make_item(self.service)
        item = bring_to_coordinating(self.service, item["id"])
        # 更晚来源只给频率，强度/带宽不变：仍是新依据，但不作废、不回退
        add_source(self.service, item["id"], "OBS-FREQ", "2026-09-27T11:30:00+00:00",
                   -35, bandwidth=20.0, frequency=2450.0)
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "coordinating")
        # 协调阶段没有未执行授权；协议仍在
        self.assertEqual(item["payload"]["coordination"]["agreement"], "AGC-7")
        self.assertEqual(item["payload"]["basis"]["frequency_mhz"], 2450.0)

    def test_late_source_after_resolved_only_enters_history(self):
        item = make_item(self.service)
        item = bring_to_coordinating(self.service, item["id"])
        item = self.service.act(item["id"], "resolve",
                                {"measurement_cleared": True, "evidence": "scan-final"},
                                "coord-1", "coordinator", item["version"], "north")
        self.assertEqual(item["status"], "resolved")
        result = add_source(self.service, item["id"], "OBS-AFTER-CLOSE",
                            "2026-10-01T00:00:00+00:00", -20, bandwidth=40.0)
        self.assertFalse(result["promoted"])
        self.assertEqual(result["promotion"]["reason"], "terminal_history_only")
        item = self.service.get_item(item["id"])
        self.assertEqual(item["status"], "resolved")
        self.assertEqual(item["payload"]["basis"]["external_id"], "OBS-1")
        self.assertEqual(len(item["sources"]), 2)

    def test_duplicate_authorization_rejected(self):
        item = make_item(self.service)
        item = bring_to_coordinating(self.service, item["id"])
        add_source(self.service, item["id"], "OBS-REOPEN", "2026-09-27T11:00:00+00:00", -72)
        item = self.service.get_item(item["id"])
        item = self.service.act(item["id"], "locate", {"location": "cell-9", "confidence": 0.95},
                                "field-1", "field_operator", item["version"], "north")
        item = self.service.act(item["id"], "authorize_suspend", {"authorization_code": "REG-NORTH-3"},
                                "coord-2", "coordinator", item["version"], "north")
        with self.assertRaises(DomainError) as exc:
            self.service.act(item["id"], "authorize_suspend", {"authorization_code": "REG-NORTH-4"},
                             "coord-2", "coordinator", item["version"], "north")
        self.assertEqual(exc.exception.code, "authorization_pending")


if __name__ == "__main__":
    unittest.main()
