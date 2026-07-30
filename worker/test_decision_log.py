import unittest

from decision_log import RULE_VERSION, build_record, prefilter_reasons


CONSTANTS = {
    "direct_watch_min": 120,
    "direct_go_min": 200,
    "fallback_radar_min": 90,
    "fallback_rain_distance_max": 15,
    "fallback_observer_rain_max": 0.05,
    "optimal_sun_min": 5,
    "optimal_sun_max": 22,
}


class DecisionLogTests(unittest.TestCase):
    def test_rejection_record_preserves_replayable_features(self):
        candidate = {
            "lat": 39.3256, "lon": -76.7651, "rainLat": 39.295, "rainLon": -76.595,
            "rainDistanceKm": 25, "rainRateMmHr": 3.1, "observerRainRateMmHr": 0,
            "sunElevationDeg": 14.38, "sunBearingDeg": 283.1,
            "antiSolarBearingDeg": 103.1, "radarScore": 94.4,
            "antiSolarRainSpanByTier": {"any": {"arcSpanDeg": 20, "occupiedDeg": 24, "segmentCount": 2}},
            "dniWm2": 0, "cloudCoverPct": 100, "sunlightObservedAt": "2026-07-28T23:00:00Z",
        }
        reasons = prefilter_reasons(candidate, CONSTANTS)
        record = build_record(
            candidate, "2026-07-28T23:02:00Z", "rejected", "model_prefilter",
            reasons, CONSTANTS,
        )
        self.assertEqual(RULE_VERSION, "noaa-detector-2026-07-v1")
        self.assertIn("model_dni_below_watch", reasons)
        self.assertIn("geometry_fallback_rain_too_distant", reasons)
        self.assertEqual(record["features"]["geometry"]["radarScore"], 94.4)
        self.assertEqual(record["features"]["rain"]["distanceKm"], 25)
        self.assertEqual(record["features"]["rain"]["antiSolarRainSpanByTier"]["any"]["arcSpanDeg"], 20)
        self.assertEqual(record["features"]["model"]["directNormalIrradianceWm2"], 0)
        self.assertIsNone(record["features"]["satellite"]["dsrfClearnessRatio"])
        self.assertIsNone(record["features"]["sunlightV2"])
        self.assertEqual(record["features"]["thresholdSnapshot"]["geometryFallbackObserverRainMaxMmHr"], 0.05)

    def test_candidate_id_is_stable_below_declared_coordinate_precision(self):
        common = {"lat": 39.32564, "lon": -76.76514, "rainLat": 39.29504, "rainLon": -76.59504}
        noisy = {"lat": 39.325641, "lon": -76.765141, "rainLat": 39.295041, "rainLon": -76.595041}
        first = build_record(common, "2026-07-28T23:02:00Z", "rejected", "model_prefilter", ["model_data_unavailable"], CONSTANTS)
        second = build_record(noisy, "2026-07-28T23:02:00Z", "rejected", "model_prefilter", ["model_data_unavailable"], CONSTANTS)
        self.assertEqual(first["candidateId"], second["candidateId"])

    def test_satellite_values_remain_raw_and_versionable(self):
        candidate = {"lat": 35, "lon": -86, "rainLat": 35, "rainLon": -85.9, "sunElevationDeg": 8.5}
        record = build_record(
            candidate, "2026-07-28T23:02:00Z", "selected_possible", "final_selection",
            ["selected_model_possible"], CONSTANTS,
            goes={"sunlightDecision": "watch"},
            sources={
                "goesDsrf": {"value": 82.5, "observedAt": "2026-07-28T23:00:00Z", "fresh": True, "dqf": 1, "dqfMeaning": "degraded_or_invalid", "quantitativeSolarZenithBoundsDeg": [0, 70], "retrievalSolarZenithBoundsDeg": [0, 90]},
                "goesAcmc": {"value": 2, "observedAt": "2026-07-28T23:01:00Z", "fresh": True},
            },
        )
        satellite = record["features"]["satellite"]
        self.assertEqual(satellite["dsrf"]["valueWm2"], 82.5)
        self.assertEqual(satellite["dsrf"]["solarZenithDeg"], 81.5)
        self.assertFalse(satellite["dsrf"]["usable"])
        self.assertEqual(satellite["dsrf"]["unavailableReason"], "outside_quantitative_solar_zenith_range")
        self.assertEqual(satellite["acmc"]["value"], 2)
        self.assertEqual(satellite["decision"], "watch")

    def test_good_quality_in_bounds_dsrf_is_logged_as_usable(self):
        candidate = {"lat": 35, "lon": -86, "rainLat": 35, "rainLon": -85.9, "sunElevationDeg": 20}
        record = build_record(
            candidate, "2026-07-28T23:02:00Z", "selected_possible", "final_selection",
            ["selected_model_possible"], CONSTANTS,
            goes={"sunlightDecision": "watch"},
            sources={"goesDsrf": {"value": 180, "dqf": 0, "dqfMeaning": "good_quality", "quantitativeSolarZenithBoundsDeg": [0, 70]}},
        )
        dsrf = record["features"]["satellite"]["dsrf"]
        self.assertTrue(dsrf["usable"])
        self.assertIsNone(dsrf["unavailableReason"])
        self.assertEqual(record["features"]["satellite"]["decision"], "watch")


if __name__ == "__main__":
    unittest.main()
