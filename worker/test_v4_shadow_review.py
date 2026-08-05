import unittest

from v4_shadow_review import MODEL_VERSION, RULE_VERSION, finalize_v42_records, mark_v42_dispatched, select_v4_shadow_records


def sidecar(scan, radar_scores):
    envelope, core, seeds = [], [], []
    for index, score in enumerate(radar_scores):
        column = 2 + index * 3
        envelope.append([1, column, column, 1])
        core.append([1, column, column, 1])
        seeds.append({
            "candidateId": f"seed-{index}", "lat": 1.0, "lon": float(column - 1),
            "rainLat": 1.0, "rainLon": float(column), "rainDistanceKm": 10.0,
            "rainRateMmHr": 2.0, "observerRainRateMmHr": 0.0,
            "spatialSupport": {"connectedWetCellCount": 2},
            "sunElevationDeg": 10.0, "sunBearingDeg": 270.0,
            "antiSolarBearingDeg": 90.0, "radarScore": score,
        })
    observed = f"2026-08-05T12:{scan * 5:02d}:00Z"
    return {
        "observedAt": observed, "rainFootprintId": f"fp-{scan}",
        "grid": {"latitudeStart": 0.0, "latitudeStepDeg": 1.0, "latitudeCount": 3,
                 "longitudeStart": 0.0, "longitudeStepDeg": 1.0, "longitudeCount": 100},
        "stormSegmentation": {"envelopeRuns": envelope, "coreRuns": core},
        "candidateSeeds": seeds,
    }


class V4ShadowReviewTests(unittest.TestCase):
    def test_all_eligible_families_are_frozen_even_without_camera_matches(self):
        previous = None
        for scan in range(3):
            result = select_v4_shadow_records(sidecar(scan, [99, 98, 97]), previous, [])
            previous = {"v4ShadowState": result["state"]}
        self.assertEqual(result["modelVersion"], MODEL_VERSION)
        self.assertEqual(result["eligibleFamilies"], 3)
        self.assertEqual(result["ruleVersion"], RULE_VERSION)
        self.assertEqual(result["retainedEligible"], 3)
        self.assertEqual(len(result["records"]), 3)
        self.assertTrue(all(item["features"]["researchReview"]["lane"] == "evaluation_pending" for item in result["records"]))
        self.assertTrue(all(not item["features"]["researchReview"]["hasMatchedCamera"] for item in result["records"]))
        self.assertTrue(all(len(item["features"]["researchReview"]["v4Prediction"]["predictionSha256"]) == 64 for item in result["records"]))

    def test_prediction_manifest_is_deterministic(self):
        prior = None
        for scan in range(2):
            step = select_v4_shadow_records(sidecar(scan, [99, 98, 97, 96, 95, 94, 93]), prior, [])
            prior = {"v4ShadowState": step["state"]}
        left = select_v4_shadow_records(sidecar(2, [99, 98, 97, 96, 95, 94, 93]), prior, [])
        right = select_v4_shadow_records(sidecar(2, [99, 98, 97, 96, 95, 94, 93]), prior, [])
        self.assertEqual(left["predictionManifestSha256"], right["predictionManifestSha256"])
        self.assertEqual(left["predictions"], right["predictions"])

    def test_sunlight_classifies_every_record_and_withholds_only_overcast(self):
        previous = None
        for scan in range(3):
            selected = select_v4_shadow_records(sidecar(scan, [99, 98, 97, 96]), previous, [])
            previous = {"v4ShadowState": selected["state"]}
        states = ["sunlit_supported", "sunlight_plausible", "unresolved", "overcast_supported"]
        for record, sunlight_state in zip(selected["records"], states):
            record["features"]["sunlightV2"] = {
                "methodVersion": "sunlight-test", "sunlightState": sunlight_state,
                "sunlightStateReasons": ["test"],
            }
        finalized = finalize_v42_records(selected["records"], selected["state"], "2026-08-05T12:12:00Z")
        self.assertEqual(finalized["classificationCounts"], {
            "GO_SUNLIT_SUPPORTED": 1, "POSSIBLE_PLAUSIBLE": 1,
            "POSSIBLE_UNRESOLVED": 1, "WITHHELD_OVERCAST": 1,
        })
        self.assertEqual(len(finalized["records"]), 4)
        self.assertEqual(len(finalized["dispatchRecords"]), 3)
        self.assertEqual([item["features"]["researchReview"]["lane"] for item in finalized["records"]],
                         ["go", "possible", "possible", "withheld"])

    def test_same_family_classification_dispatches_once_per_day(self):
        previous = None
        for scan in range(3):
            selected = select_v4_shadow_records(sidecar(scan, [99]), previous, [])
            previous = {"v4ShadowState": selected["state"]}
        record = selected["records"][0]
        record["features"]["sunlightV2"] = {"sunlightState": "sunlit_supported"}
        first = finalize_v42_records([record], selected["state"], "2026-08-05T12:12:00Z")
        self.assertEqual(len(first["dispatchRecords"]), 1)
        mark_v42_dispatched(selected["state"], first["dispatchRecords"], "2026-08-05T12:12:00Z")
        next_record = select_v4_shadow_records(sidecar(3, [99]), {"v4ShadowState": selected["state"]}, [])["records"][0]
        next_record["features"]["sunlightV2"] = {"sunlightState": "sunlit_supported"}
        second = finalize_v42_records([next_record], selected["state"], "2026-08-05T12:17:00Z")
        self.assertEqual(second["dispatchRecords"], [])


if __name__ == "__main__":
    unittest.main()
