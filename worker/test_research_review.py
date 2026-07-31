import unittest
from research_review import RULE_VERSION, eligibility, select_research_candidates


def record(candidate_id="candidate", *, score=65, elevation=3.3, distance=8,
           observer_rain=0, span=100, state="sunlit_supported", disposition="rejected"):
    return {"candidateId": candidate_id, "disposition": disposition,
            "decisionReasons": ["model_dni_below_watch"], "features": {
        "observer": {"lat": 41.12, "lon": -112.08},
        "rain": {"distanceKm": distance, "observerRateMmHr": observer_rain,
                 "antiSolarRainSpanByTier": {"tier2Plus": {"arcSpanDeg": span}}},
        "geometry": {"radarScore": score, "sunElevationDeg": elevation, "antiSolarBearingDeg": 112.3},
        "sunlightV2": {"sunlightState": state}}}


class ResearchReviewTests(unittest.TestCase):
    def test_ogden_shape_is_selected_despite_old_sunlight_rejection(self):
        item = record()
        self.assertTrue(eligibility(item)[0])
        self.assertEqual(select_research_candidates([item])["selected"], 1)
        self.assertEqual(item["disposition"], "selected_research_possible")
        self.assertEqual(item["features"]["researchReview"]["ruleVersion"], RULE_VERSION)

    def test_affirmative_overcast_blocks_but_plausible_does_not(self):
        blocked, plausible = record("blocked", state="overcast_supported"), record("plausible", state="sunlight_plausible")
        self.assertEqual(select_research_candidates([blocked, plausible])["selectedCandidateIds"], ["plausible"])

    def test_shadow_candidates_are_capped_before_the_two_slot_ui_gate(self):
        items = [record(str(index), score=90-index) for index in range(10)]
        self.assertEqual(select_research_candidates(items)["selected"], 6)

    def test_narrow_rain_or_wet_observer_is_not_selected(self):
        self.assertFalse(eligibility(record(span=29))[0])
        self.assertFalse(eligibility(record(observer_rain=0.06))[0])

    def test_camera_gate_keeps_ogden_and_excludes_camera_deserts(self):
        catalog = [{"id": 969, "name": "Ogden", "lat": 41.193604, "lon": -112.00825,
                    "cameras": [{"id": 13590, "bearing": 90, "direction": "East"}]}]
        ogden = record("ogden")
        desert = record("desert")
        desert["features"]["observer"] = {"lat": 35, "lon": -100}
        result = select_research_candidates([desert, ogden], camera_catalog=catalog)
        self.assertEqual(result["selectedCandidateIds"], ["ogden"])
        self.assertEqual(ogden["features"]["researchReview"]["cameraMatches"][0]["cameraId"], 13590)
        self.assertEqual(ogden["originalDisposition"], "rejected")
        self.assertEqual(ogden["features"]["researchReview"]["originalDisposition"], "rejected")

    def test_camera_gate_rejects_unknown_zero_fov_and_more_than_40km(self):
        item = record("camera-guards")
        invalid = [
            {"id": 1, "name": "Unknown bearing", "lat": 41.12, "lon": -112.08,
             "cameras": [{"id": 1, "direction": "unknown"}]},
            {"id": 2, "name": "Zero FOV", "lat": 41.12, "lon": -112.08,
             "cameras": [{"id": 2, "bearing": 112.3, "mapWedgeAngle": 0}]},
            {"id": 3, "name": "Too far", "lat": 41.6, "lon": -112.08,
             "cameras": [{"id": 3, "bearing": 112.3, "mapWedgeAngle": 45}]},
        ]
        self.assertEqual(select_research_candidates([item], camera_catalog=invalid)["selected"], 0)


if __name__ == "__main__": unittest.main()
