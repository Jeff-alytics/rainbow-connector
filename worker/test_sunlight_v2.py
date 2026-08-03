import csv
import gzip
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

API_DIR = Path(__file__).resolve().parents[1] / 'api'
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from decision_store import build_envelope
from sunlight_v2 import (
    DSRF_CLEAR_SKY_METHOD_VERSION, METHOD_VERSION, combine_shadow, dsrf_shadow,
    cleanup_shadow_cache, nearest_metars, parse_metar_cache, safe_enrich_sunlight_v2,
    sunlight_state,
)


def metar_payload():
    header = [
        "raw_text", "station_id", "observation_time", "latitude", "longitude",
        "visibility_statute_mi", "wx_string", "sky_cover", "cloud_base_ft_agl",
        "sky_cover", "cloud_base_ft_agl", "flight_category",
    ]
    rows = [
        ["METAR KAAA 282300Z SCT020 BKN080", "KAAA", "2026-07-28T23:00:00Z", "39.0", "-76.0", "10", "-RA", "SCT", "2000", "BKN", "8000", "VFR"],
        ["METAR KBBB 282300Z OVC010", "KBBB", "2026-07-28T23:00:00Z", "42.0", "-80.0", "5", "RA", "OVC", "1000", "", "", "IFR"],
    ]
    target = io.StringIO(); writer = csv.writer(target); writer.writerow(header); writer.writerows(rows)
    return gzip.compress(target.getvalue().encode())


class SunlightV2Tests(unittest.TestCase):
    def test_metar_cache_preserves_repeated_cloud_layers_and_nearest_station(self):
        observations = parse_metar_cache(metar_payload())
        self.assertEqual(len(observations[0]["cloudLayers"]), 2)
        evidence = nearest_metars({"lat": 39.01, "lon": -76.01}, observations, "2026-07-28T23:02:00Z")
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["stations"][0]["stationId"], "KAAA")
        self.assertEqual(evidence["stations"][0]["cloudLayers"][0]["cover"], "SCT")
        self.assertGreater(evidence["supportScore"], 0.5)

    def test_metar_after_assessment_cutoff_is_excluded(self):
        observations = parse_metar_cache(metar_payload())
        evidence = nearest_metars(
            {"lat": 39.01, "lon": -76.01}, observations, "2026-07-28T22:55:00Z",
            available_by="2026-07-28T22:58:00Z",
        )
        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["excludedAfterCutoff"], 1)

    def test_dsrf_ratio_only_exists_for_quality_valid_retrieval(self):
        valid = dsrf_shadow({"value": 200, "dqf": 0, "quantitativeSolarZenithBoundsDeg": [0, 70]}, 25)
        invalid = dsrf_shadow({"value": 3, "dqf": 0, "quantitativeSolarZenithBoundsDeg": [0, 70]}, 8.5)
        self.assertIsNotNone(valid["clearnessRatio"])
        self.assertEqual(valid["clearSkyMethodVersion"], DSRF_CLEAR_SKY_METHOD_VERSION)
        self.assertIsNone(invalid["clearnessRatio"])
        self.assertEqual(invalid["unavailableReason"], "outside_quantitative_solar_zenith_range")

    def test_combiner_renormalizes_only_available_components(self):
        probability, components = combine_shadow(
            {"maxGapFraction": 0.8}, {"supportScore": 0.6},
            {"supportScore": None}, {"clearnessRatio": None},
        )
        self.assertAlmostEqual(probability, (0.8 * 0.45 + 0.6 * 0.25) / 0.70)
        self.assertEqual(set(components), {"band2", "acmc"})

    def test_empty_scan_does_no_network_work(self):
        result = safe_enrich_sunlight_v2([], [], "2026-07-28T23:02:00Z", None)
        self.assertTrue(result["skipped"])

    def test_shadow_downloads_are_removed_after_each_run(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, "band2", "frame.nc")
            path.parent.mkdir(); path.write_bytes(b"temporary")
            cleanup_shadow_cache(Path(directory))
            self.assertFalse(path.exists())

    def test_envelope_reports_disagreement_and_missing_by_disposition(self):
        artifact = {"generatedAt": "2026-07-28T23:04:00Z", "sourceHealth": {"radar": {"observedAt": "2026-07-28T23:02:00Z"}}}
        records = [
            {"disposition": "selected_possible", "features": {"sunlightV2": {"disagreesWithV1": True}}},
            {"disposition": "selected_possible", "features": {"sunlightV2": {"disagreesWithV1": False}}},
            {"disposition": "rejected", "features": {"sunlightV2": None}},
        ]
        metrics = build_envelope(artifact, records)["metrics"]["v1V2DisagreementRateByDisposition"]
        self.assertEqual(metrics["selected_possible"]["rate"], 0.5)
        self.assertEqual(metrics["rejected"]["missing"], 1)

    def test_method_is_versioned_and_non_operational_by_contract(self):
        self.assertEqual(METHOD_VERSION, "sunlight-v2-shadow-2026-08-v4")

    def test_four_state_requires_corroboration_and_reserves_dark_for_supported_overcast(self):
        supported, _ = sunlight_state(
            {"temporalFramesUsed": 3, "corroboratingFrames": 2, "causalGapFraction": 0.7, "maximumFrameGapFraction": 0.8},
            {"supportScore": 0.0}, {"available": True, "supportScore": 0.3}, {"clearnessRatio": None},
        )
        overcast, _ = sunlight_state(
            {"temporalFramesUsed": 3, "corroboratingFrames": 0, "causalGapFraction": 0.0, "maximumFrameGapFraction": 0.0},
            {"supportScore": 0.0}, {"available": True, "supportScore": 0.3}, {"clearnessRatio": None},
        )
        unresolved, _ = sunlight_state(
            {"temporalFramesUsed": 1, "corroboratingFrames": 0, "causalGapFraction": 0.0, "maximumFrameGapFraction": 0.0},
            {"supportScore": 0.0}, {"available": False, "supportScore": None}, {"clearnessRatio": None},
        )
        missing_metar, _ = sunlight_state(
            {"temporalFramesUsed": 3, "corroboratingFrames": 0, "causalGapFraction": 0.0, "maximumFrameGapFraction": 0.0},
            {"supportScore": 0.0}, {"available": False, "supportScore": None}, {"clearnessRatio": None},
        )
        self.assertEqual(supported, "sunlit_supported")
        self.assertEqual(overcast, "overcast_supported")
        self.assertEqual(unresolved, "unresolved")
        self.assertEqual(missing_metar, "unresolved")


if __name__ == "__main__":
    unittest.main()
