import ast
import json
import unittest
from pathlib import Path

from scripts.build_time_aligned_storm_targets import SITE_ID

ROOT = Path(__file__).resolve().parents[1]


class TimeAlignedStormTargetTests(unittest.TestCase):
    def test_site_id_pattern_extracts_digits(self):
        self.assertEqual(SITE_ID.match("site1013-20260727T120000Z").group(1), "1013")

    def test_builder_has_no_prediction_or_evaluator_import(self):
        path = ROOT / "scripts" / "build_time_aligned_storm_targets.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(any(
            "prediction" in name or "evaluate" in name or "storm_object_replay" in name
            for name in imports
        ), imports)

    def test_frozen_diagnostic_config_has_no_lower_distance_floor(self):
        config = json.loads(
            (ROOT / "validation/storm-object-replay-gate/time-aligned-target-config-v3.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(config["envelopeDistancePrefilterKm"], [0, 45])
        self.assertEqual(config["originalFrozenGateStatus"], "failed_and_unchanged")


if __name__ == "__main__":
    unittest.main()
