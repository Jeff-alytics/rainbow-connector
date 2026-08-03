"""Protection tests for the historical review queue builder.

Covers the handoff's required protections that live in this stage:
event-level exclusions must catch neighboring cameras in a frozen storm (not
just the original case), tier boundaries must match the production constants,
and event grouping must split on the lineage gap.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "worker", ROOT / "api"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from build_historical_review_queue import (build_events, classify_disposition, excluded_by,
                                           frozen_exclusions)


def seed(observed_at, lat=39.30, lon=-76.60, site="900", radar=80.0):
    return {"observedAt": observed_at, "lat": lat, "lon": lon, "rainLat": lat, "rainLon": lon,
            "rainDistanceKm": 15, "rainRateMmHr": 2.0, "observerRainRateMmHr": 0.0,
            "sunElevationDeg": 12.0, "sunBearingDeg": 285.0, "antiSolarBearingDeg": 105.0,
            "radarScore": radar,
            "cameraMatches": [{"siteId": site, "cameraId": "1", "name": "Test", "direction": "E",
                               "distanceKm": 10.0, "visibleBowFraction": 0.8, "bowArcOverlapDeg": 30.0}]}


class ExclusionTests(unittest.TestCase):
    def test_neighboring_camera_in_frozen_storm_is_excluded(self):
        rules = frozen_exclusions()
        # ~40 km from the Baltimore observation, inside the padded window: a
        # neighboring camera seeing the same storm must be excluded.
        neighbor = build_events([seed("2026-07-28T23:30:00Z", lat=39.55, lon=-76.30)])[0]
        self.assertIsNotNone(excluded_by(neighbor, rules))
        # Same place, three days earlier: time window must not leak.
        earlier = build_events([seed("2026-07-25T23:30:00Z", lat=39.55, lon=-76.30)])[0]
        self.assertIsNone(excluded_by(earlier, rules))
        # Same window, far away (Kansas): radius must not leak.
        far = build_events([seed("2026-07-28T23:30:00Z", lat=38.5, lon=-98.0)])[0]
        self.assertIsNone(excluded_by(far, rules))

    def test_all_six_frozen_cases_present(self):
        sources = {rule["source"].split(":")[1].rsplit("-", 1)[0] for rule in frozen_exclusions()}
        self.assertGreaterEqual(len(sources), 6, sources)


class DispositionTests(unittest.TestCase):
    def test_production_go_requires_all_three_gates(self):
        self.assertEqual(classify_disposition(13.0, 250.0, "go"), "go")
        self.assertEqual(classify_disposition(13.0, 199.9, "go"), "possible")   # dni below go min
        self.assertEqual(classify_disposition(28.0, 250.0, "go"), "possible")   # outside optimal sun
        self.assertEqual(classify_disposition(13.0, 250.0, "watch"), "possible")

    def test_near_miss_band_is_labelled_not_selected(self):
        self.assertEqual(classify_disposition(13.0, 100.0, "go"), "near_miss")
        self.assertEqual(classify_disposition(13.0, 250.0, "unknown"), "near_miss")
        self.assertEqual(classify_disposition(13.0, 50.0, "go"), "rejected")
        self.assertEqual(classify_disposition(13.0, 100.0, "blocked"), "rejected")

    def test_blocked_satellite_never_selects(self):
        self.assertEqual(classify_disposition(13.0, 400.0, "blocked"), "rejected")


class EventGroupingTests(unittest.TestCase):
    def test_splits_on_gap_over_30_minutes(self):
        rows = [seed("2026-07-10T22:00:00Z"), seed("2026-07-10T22:20:00Z"),
                seed("2026-07-10T23:10:00Z")]  # 50-min gap before the third
        events = build_events(rows)
        self.assertEqual([event["scanCount"] for event in events], [2, 1])
        self.assertTrue(events[0]["persistent"])
        self.assertFalse(events[1]["persistent"])

    def test_best_scan_is_max_radar_score(self):
        rows = [seed("2026-07-10T22:00:00Z", radar=60), seed("2026-07-10T22:10:00Z", radar=95)]
        event = build_events(rows)[0]
        self.assertEqual(event["bestScan"]["radarScore"], 95)


if __name__ == "__main__":
    unittest.main()
