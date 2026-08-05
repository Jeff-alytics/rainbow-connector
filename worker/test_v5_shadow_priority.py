import unittest

from v5_shadow_priority import RULE_VERSION, compact_v5_feed, score_v5_shadow_records


SCAN = "2026-08-05T12:00:00Z"


def seed(candidate, radar=90, adjacent=4, span=45, elevation=10, observer_rain=0, lat=0.1):
    return {
        "candidateId": candidate, "lat": lat, "lon": 0.1, "rainLat": 0, "rainLon": 0,
        "rainDistanceKm": 8, "rainRateMmHr": 2, "observerRainRateMmHr": observer_rain,
        "sunElevationDeg": elevation, "sunBearingDeg": 270, "antiSolarBearingDeg": 90,
        "radarScore": radar, "spatialSupport": {"adjacentWetCells": adjacent, "isolatedPixel": adjacent == 0},
        "antiSolarRainArcSpanDeg": span,
    }


def sidecar(seeds):
    return {"observedAt": SCAN, "grid": {"latitudeStart": 0, "latitudeStepDeg": 1,
        "longitudeStart": 0, "longitudeStepDeg": 1}, "v5ExpandedSeeds": seeds,
        "upstreamSeedAudit": {"expanded": {"candidateCap": None, "samplingStride": 10}}}


def storm(scans=4):
    return {"stormObjects": [{"componentId": "component-a", "eventId": "family-a",
        "ancestorEventIds": ["family-a"], "lineageScanCount": scans, "runs": [[0, 0, 0, 1]]}]}


def v4_record():
    return {"candidateId": "v4", "features": {
        "observer": {"lat": 0.1, "lon": 0.1}, "rain": {"lat": 0, "lon": 0},
        "researchReview": {"familyEventId": "family-a", "componentId": "component-a",
            "v4Prediction": {"predictionSha256": "a" * 64, "classification": "GO_SUNLIT_SUPPORTED",
                "sunlightState": "sunlit_supported", "assessmentProcessingAt": "2026-08-05T12:01:00Z"}},
    }}


def score(seeds, previous=None, when=SCAN, scans=4, v4=None):
    return score_v5_shadow_records(sidecar(seeds), v4 or [], previous, when, storm(scans))


class V5ShadowPriorityTests(unittest.TestCase):
    def test_dual_cohort_retains_all_geometries_without_cap(self):
        seeds = [seed(f"s{index}", radar=90-index, lat=0.1 + index / 1000) for index in range(12)]
        result = score(seeds, v4=[v4_record()])
        self.assertEqual(result["ruleVersion"], RULE_VERSION)
        self.assertEqual(result["retained"], 12)
        self.assertEqual(result["overlap"], 1)
        self.assertEqual(result["expandedOnly"], 11)
        self.assertTrue(result["upstreamAudit"]["cohortACountParity"]["passed"])
        self.assertEqual(sorted(item["rankWithinScan"] for item in result["predictions"]), list(range(1, 13)))

    def test_overlap_prediction_is_attached_to_actual_v4_record(self):
        v4 = v4_record()
        result = score([seed("one")], v4=[v4])
        prediction = v4["features"]["researchReview"]["v5Prediction"]
        self.assertIn("overlap", prediction["cohortMembership"])
        self.assertEqual(prediction["predictionId"], result["predictions"][0]["predictionId"])

    def test_peak_is_bounded_to_trailing_window(self):
        old = score([seed("old", radar=100)], when="2026-08-05T11:00:00Z")
        previous = {"v5ShadowState": old["state"]}
        result = score([seed("now", radar=80)], previous=previous, when=SCAN)
        self.assertEqual(result["predictions"][0]["features"]["peakRadarBounded"], 80)
        self.assertEqual(result["predictions"][0]["features"]["trailingEdgeDecay"], 0)

    def test_current_rain_plausibility_never_decreases_with_stronger_current_rain(self):
        weak = score([seed("weak", radar=50)])["predictions"][0]
        strong = score([seed("strong", radar=85)])["predictions"][0]
        self.assertGreater(strong["score"], weak["score"])

    def test_wet_isolated_and_outer_solar_geometries_are_features_not_gates(self):
        result = score([seed("audit", adjacent=0, elevation=35, observer_rain=1)])
        prediction = result["predictions"][0]
        self.assertEqual(result["retained"], 1)
        self.assertEqual(prediction["solarLane"], "physical_audit")
        self.assertTrue(prediction["auditStrata"]["observerWet"])
        self.assertTrue(prediction["auditStrata"]["resolvedIsolated"])

    def test_observed_population_elevation_lanes_are_explicit(self):
        result = score([seed("core", elevation=12, lat=0.1), seed("extended", elevation=30, lat=0.11),
            seed("audit", elevation=40, lat=0.12)])
        lanes = {item["candidateId"]: item["solarLane"] for item in result["predictions"]}
        self.assertEqual(set(lanes.values()), {"observed_core", "observed_extended", "physical_audit"})
        self.assertEqual(result["predictions"][0]["solarLane"], "observed_core")

    def test_missing_spatial_uses_neutral_point_and_reports_sensitivity(self):
        prediction = score([seed("missing", adjacent=None, span=None)])["predictions"][0]
        self.assertEqual(prediction["features"]["spatialEvidenceStatus"], "unresolved")
        self.assertLess(prediction["scoreSensitivityLow"], prediction["score"])
        self.assertGreater(prediction["scoreSensitivityHigh"], prediction["score"])

    def test_feed_contains_all_predictions_and_never_enables_acquisition(self):
        result = score([seed("a"), seed("b", radar=80, lat=0.2)])
        feed = compact_v5_feed(result)
        self.assertEqual(feed["schemaVersion"], "v5-candidate-scan.v1")
        self.assertEqual(len(feed["items"]), 1)
        self.assertEqual(feed["items"][0]["geometrySelections"], 2)
        self.assertEqual(len(feed["items"][0]["geometries"]), 2)
        self.assertEqual(len({item["geometryId"] for item in feed["items"][0]["geometries"]}), 2)
        self.assertEqual(feed["retainedGeometries"], 2)
        self.assertFalse(feed["acquisitionEnabled"])
        self.assertTrue(all(item["acquisitionEnabled"] is False for item in feed["items"]))

    def test_valid_seed_without_storm_object_is_retained_as_fallback_family(self):
        result = score_v5_shadow_records(sidecar([seed("light")]), [], None, SCAN, {"stormObjects": []})
        self.assertEqual(result["retained"], 1)
        self.assertEqual(result["upstreamAudit"]["unattachedExpandedSeeds"], 1)
        self.assertEqual(result["upstreamAudit"]["retainedUnattachedSeeds"], 1)
        self.assertTrue(result["predictions"][0]["familyEventId"].startswith("v5-unattached-family-"))


if __name__ == "__main__":
    unittest.main()
