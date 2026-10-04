import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_strong_signal_and_bandwidth_raise_score(self):
        strong = assess({"strength_dbm": -30, "bandwidth_mhz": 20})
        weak = assess({"strength_dbm": -90, "bandwidth_mhz": 0.1})
        self.assertGreater(strong["score"], weak["score"])
        self.assertEqual(strong["level"], "critical")
        self.assertEqual(weak["level"], "low")

    def test_low_confidence_cannot_be_located(self):
        item = {"status": "assessed", "payload": {"strength_dbm": -40, "bandwidth_mhz": 10}}
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "locate", {"location": "x", "confidence": 0.2}, "a", "analyst")
        self.assertEqual(context.exception.code, "low_location_confidence")


if __name__ == "__main__":
    unittest.main()
