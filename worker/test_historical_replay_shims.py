import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

API_DIR = Path(__file__).resolve().parents[1] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from goes_sample import scan_age_minutes
from decision_log import candidate_id
from sunlight_v2 import nearest_metars, enrich_sunlight_v2


class HistoricalReplayShimTests(unittest.TestCase):
    def test_age_uses_pinned_replay_reference_not_wall_clock(self):
        with patch("goes_sample.utc_now", return_value=__import__("datetime").datetime(2026, 8, 2, tzinfo=__import__("datetime").timezone.utc)):
            self.assertEqual(scan_age_minutes("2026-07-28T23:00:00Z", "2026-07-28T23:15:00Z"), 15)

    def test_archived_metar_method_version_is_preserved(self):
        evidence = nearest_metars(
            {"lat": 39.0, "lon": -76.0},
            [{"stationId": "KAAA", "lat": 39.0, "lon": -76.0, "observedAt": "2026-07-28T23:00:00Z", "cloudLayers": [{"cover": "SCT"}], "rawText": "METAR KAAA SCT020"}],
            "2026-07-28T23:02:00Z",
            available_by="2026-07-28T23:05:00Z",
            method_version="iem-asos-archive-replay-v1",
        )
        self.assertEqual(evidence["methodVersion"], "iem-asos-archive-replay-v1")

    def test_injected_metars_bypass_live_fetch_and_pin_processing_time(self):
        candidate = {"lat": 39.0, "lon": -76.0, "sunElevationDeg": 12.0}
        radar_at = "2026-07-28T23:02:00Z"
        record = {"candidateId": candidate_id(candidate, radar_at), "disposition": "selected_possible", "features": {"model": {}, "satellite": {}}}
        fake_band2 = MagicMock()
        fake_band2.sample_candidate.return_value = {"temporalFramesUsed": 2, "corroboratingFrames": 0, "causalGapFraction": 0.0, "maximumFrameGapFraction": 0.0, "supportScore": None}
        fake_session = MagicMock()
        with patch("sunlight_v2.fetch_metars", side_effect=AssertionError("live METAR fetch used")) as fetch, \
             patch("sunlight_v2.sample", return_value={"value": 200, "observedAt": radar_at, "createdAt": "2026-07-28T23:03:00Z", "causalAvailableBy": "2026-07-28T23:10:00Z", "causalEligible": True, "dqf": 0, "quantitativeSolarZenithBoundsDeg": [0, 70], "pixel": {"x": 1, "y": 1}}), \
             patch("sunlight_v2.dsrf_shadow", return_value={"clearnessRatio": 0.8, "usable": True}), \
             patch("sunlight_v2.acmc_neighborhood", return_value={"supportScore": 0.8}), \
             patch("sunlight_v2.Band2Sampler", return_value=fake_band2):
            result = enrich_sunlight_v2(
                [record], [candidate], radar_at, Path("C:/tmp/replay-shim-test"), session=fake_session,
                assessment_processing_at="2026-07-28T23:10:00Z",
                metars=[], metar_method_version="iem-asos-archive-replay-v1",
            )
        self.assertFalse(fetch.called)
        self.assertEqual(result["assessmentProcessingAt"], "2026-07-28T23:10:00Z")
        self.assertEqual(record["features"]["sunlightV2"]["metar"]["methodVersion"], "iem-asos-archive-replay-v1")


if __name__ == "__main__":
    unittest.main()
