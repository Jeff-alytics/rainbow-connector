"""Protection tests for the frozen Gate 3 evaluator."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from causal_replay import run_streams, write_prediction_package
from evaluate_causal_replay import (
    baseline_summary, evaluate_thresholds, location_delivery_metrics, point_summary,
)


def seed(lat: float, radar: float = 99) -> dict:
    return {
        "lat": lat, "lon": -76, "rainLat": lat, "rainLon": -75.9,
        "rainDistanceKm": 8, "rainRateMmHr": 2,
        "observerRainRateMmHr": 0, "sunElevationDeg": 12,
        "radarScore": radar,
        "spatialSupport": {"isolatedPixel": False, "adjacentWetCells": 4},
    }


class GateEvaluatorTests(unittest.TestCase):
    def test_threshold_volume_is_measured_not_artificially_capped(self):
        streams = [{
            "streamId": "nationwide", "inputScope": "camera_independent_nationwide",
            "scans": [
                {"scanTime": "2026-07-03T00:00:00Z", "seeds": [seed(39), seed(41)]},
                {"scanTime": "2026-07-03T00:10:00Z", "seeds": [seed(39), seed(41)]},
            ],
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text("{}")
            package = root / "predictions"
            write_prediction_package(run_streams(streams), package, [source])
            points, support = evaluate_thresholds(
                package, {"persistenceFirst": [2000]},
                {"opportunity-missing": ["bow"]}, set(),
            )
        point = points["persistenceFirst"][0]
        self.assertEqual(point["totalAlerts"], 2)
        self.assertEqual(point["dailyAlerts"]["2026-07-03"], 2)
        self.assertEqual(support["unknown"], 0)
        self.assertGreater(support["supported"], 0)

    def test_lead_and_baseline_use_strictly_positive_minutes(self):
        target = {"eventId": "bow", "targetAt": "2026-07-03T00:20:00Z"}
        point = {
            "model": "boundedBlend", "threshold": 80, "totalAlerts": 1,
            "dailyAlerts": {"2026-07-03": 1}, "cameraLaneAlerts": 1,
            "labelCrossings": {"bow": "2026-07-03T00:10:00Z"},
            "frozenMissCrossings": {"miss": "2026-07-03T00:00:00Z"},
        }
        summary = point_summary(
            point, {"bow": target}, {"miss": "2026-07-03T00:15:00Z"}
        )
        self.assertEqual(summary["positiveLeadBowCount"], 1)
        self.assertEqual(summary["bowLeadsMinutes"]["bow"], 10)
        self.assertEqual(summary["frozenMissLeadsMinutes"]["miss"], 15)

        baseline = baseline_summary(
            {"bow": {"disposition": "go", "startAt": "2026-07-03T00:20:00Z"}},
            {"bow": "lineage"}, {"bow": target},
        )
        self.assertEqual(baseline["positiveLeadBowCount"], 0)

    def test_typical_location_metric_counts_alerts_inside_cooldown(self):
        alerts = [
            {"scanTime": "2026-07-03T00:00:00Z", "lat": 39, "lon": -76},
            {"scanTime": "2026-07-03T01:00:00Z", "lat": 39.01, "lon": -76},
            {"scanTime": "2026-07-03T01:00:00Z", "lat": 42, "lon": -76},
        ]
        metrics = location_delivery_metrics(alerts, 15, 12)
        self.assertEqual(metrics["maximumAlertsWithinCooldown"], 2)


if __name__ == "__main__":
    unittest.main()
