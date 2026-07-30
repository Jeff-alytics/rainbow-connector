import gzip
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from rain_footprint import (
    SCHEMA_VERSION, build_sidecar, candidate_rain_span, encode_runs,
    attach_record_rain_spans, occupancy_metrics, rate_tiers,
)
from rain_footprint_store import object_key, persist_rain_footprint, safe_build_and_persist_rain_footprint, safe_persist_rain_footprint
from decision_log import candidate_id


class FakeS3:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def put_object(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)


class RainFootprintTests(unittest.TestCase):
    def test_tiers_and_runs_are_lossless_and_include_heavy_cores(self):
        rates = np.array([[0, 0.05, 0.5, 1, 6, 25, np.nan], [0, 0, 2, 2, 0, 5, 5]])
        tiers = rate_tiers(rates)
        self.assertEqual(tiers.tolist(), [[0, 1, 1, 2, 3, 3, 0], [0, 0, 2, 2, 0, 3, 3]])
        self.assertEqual(encode_runs(tiers), [[0, 1, 2, 1], [0, 3, 3, 2], [0, 4, 5, 3], [1, 2, 3, 2], [1, 5, 6, 3]])

    def test_two_degree_internal_gaps_are_filled(self):
        occupied = np.zeros(12, dtype=bool)
        occupied[[1, 4, 9]] = True
        self.assertEqual(occupancy_metrics(occupied), {"arcSpanDeg": 4, "occupiedDeg": 5, "segmentCount": 2})

    def test_sidecar_metadata_and_id_are_deterministic(self):
        lats = np.array([2.0, 1.0, 0.0])
        lons = np.array([-2.0, -1.0, 0.0, 1.0])
        rates = np.zeros((3, 4))
        rates[1, 2] = 1.5
        when = datetime(2026, 7, 28, 23, 2, tzinfo=timezone.utc)
        sidecar = build_sidecar(lats, lons, rates, when, "source-key")
        self.assertEqual(sidecar["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(sidecar["rainFootprintId"], "mrms-footprint-20260728T230200Z")
        self.assertEqual(sidecar["grid"]["latitudeStepDeg"], -1.0)
        self.assertEqual(sidecar["runs"], [[1, 2, 2, 2]])
        self.assertEqual(sidecar["displayRainThresholdMmHr"], 0.2)
        self.assertEqual(sidecar["displayWetRuns"], [[1, 2, 2, 1]])

    def test_mrms_grib_coordinate_jitter_is_normalized_but_irregular_grid_is_rejected(self):
        lats = np.array([2.0, 1.990001, 1.98])
        sidecar = build_sidecar(lats, np.array([0.0, 0.01]), np.zeros((3, 2)), datetime(2026, 7, 28, tzinfo=timezone.utc), "source")
        self.assertAlmostEqual(sidecar["grid"]["latitudeStepDeg"], -0.01, places=5)
        with self.assertRaises(ValueError):
            build_sidecar(np.array([2.0, 1.99, 1.95]), np.array([0.0, 0.01]), np.zeros((3, 2)), datetime(2026, 7, 28, tzinfo=timezone.utc), "source")

    def test_candidate_span_is_tiered_and_anti_solar(self):
        lats = np.linspace(-0.4, 0.4, 81)
        lons = np.linspace(-0.4, 0.4, 81)
        rates = np.zeros((81, 81))
        # Broad rain curtain east of the observer, plus stronger central cells.
        for row, lat in enumerate(lats):
            for column, lon in enumerate(lons):
                distance = np.hypot(lat * 111, lon * 111)
                bearing = (np.degrees(np.arctan2(lon, lat)) + 360) % 360
                if 8 <= distance <= 30 and 65 <= bearing <= 115:
                    rates[row, column] = 0.5
                if 10 <= distance <= 25 and 80 <= bearing <= 100:
                    rates[row, column] = 2.0
                if 12 <= distance <= 20 and 87 <= bearing <= 93:
                    rates[row, column] = 6.0
        spans = candidate_rain_span({"lat": 0, "lon": 0, "antiSolarBearingDeg": 90}, lats, lons, rates)
        self.assertGreater(spans["any"]["arcSpanDeg"], spans["tier2Plus"]["arcSpanDeg"])
        self.assertGreater(spans["tier2Plus"]["arcSpanDeg"], spans["tier3"]["arcSpanDeg"])
        self.assertGreater(spans["tier3"]["arcSpanDeg"], 0)

    def test_deferred_span_is_merged_into_matching_decision_record(self):
        lats = np.linspace(-0.2, 0.2, 41)
        lons = np.linspace(-0.2, 0.2, 41)
        rates = np.zeros((41, 41))
        rates[18:23, 27:35] = 2.0
        candidate = {"lat": 0, "lon": 0, "rainLat": 0, "rainLon": 0.1, "antiSolarBearingDeg": 90}
        observed_at = "2026-07-28T23:02:00Z"
        record = {"candidateId": candidate_id(candidate, observed_at), "features": {"observer": {"lat": 0, "lon": 0}, "rain": {"lat": 0, "lon": 0.1}}}
        attach_record_rain_spans([record], [candidate], lats, lons, rates, observed_at)
        self.assertIn("antiSolarRainSpanByTier", record["features"]["rain"])
        self.assertGreater(record["features"]["rain"]["antiSolarRainArcSpanDeg"], 0)

    def test_deferred_span_join_does_not_depend_on_record_coordinate_precision(self):
        lats, lons = np.linspace(-0.2, 0.2, 41), np.linspace(-0.2, 0.2, 41)
        rates = np.zeros((41, 41)); rates[18:23, 27:35] = 2.0
        candidate = {"lat": 0.00004, "lon": 0.00004, "rainLat": 0, "rainLon": 0.1, "antiSolarBearingDeg": 90}
        observed_at = "2026-07-28T23:02:00Z"
        record = {"candidateId": candidate_id(candidate, observed_at), "features": {"observer": {"lat": 99, "lon": 99}, "rain": {"lat": 99, "lon": 99}}}
        attach_record_rain_spans([record], [candidate], lats, lons, rates, observed_at)
        self.assertIn("antiSolarRainSpanByTier", record["features"]["rain"])

    def test_private_store_is_deterministic_compressed_and_schema_versioned(self):
        sidecar = build_sidecar(np.array([1.0, 0.0]), np.array([0.0, 1.0]), np.array([[0, 1], [6, 0]]), datetime(2026, 7, 28, 23, 2, tzinfo=timezone.utc), "source")
        s3 = FakeS3()
        result = persist_rain_footprint(sidecar, bucket="private", s3_client=s3)
        self.assertTrue(result["ok"])
        self.assertEqual(object_key(sidecar), f"rain-footprint/rolling/{SCHEMA_VERSION}/2026/07/28/mrms-footprint-20260728T230200Z.json.gz")
        call = s3.calls[0]
        self.assertEqual(call["ServerSideEncryption"], "AES256")
        self.assertEqual(json.loads(gzip.decompress(call["Body"]))["rainFootprintId"], sidecar["rainFootprintId"])

    def test_store_failure_is_contained(self):
        result = safe_persist_rain_footprint({"observedAt": "2026-07-28T23:02:00Z", "rainFootprintId": "x"}, bucket="private", s3_client=FakeS3(RuntimeError("write failed")))
        self.assertFalse(result["ok"])
        self.assertIn("write failed", result["error"])

    def test_deferred_build_failure_is_contained(self):
        result = safe_build_and_persist_rain_footprint({"latitudes": None})
        self.assertFalse(result["ok"])

    def test_template_grants_only_rolling_sidecar_writes_and_retains_lifecycle(self):
        template = Path(__file__).resolve().parents[1].joinpath("template.yaml").read_text(encoding="utf-8")
        self.assertIn("WriteRollingRainFootprints", template)
        self.assertIn("/rain-footprint/rolling/*", template)
        self.assertIn("Prefix: rain-footprint/rolling/", template)
        self.assertIn("ExpirationInDays: 14", template)
        self.assertIn("SunlightShadowWorker", template)
        self.assertIn("lambda:InvokeFunction", template)
        self.assertIn("ReadWriteRollingDecisionLogsForShadowEnrichment", template)


if __name__ == "__main__":
    unittest.main()
