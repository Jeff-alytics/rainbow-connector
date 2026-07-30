import json
import os
import unittest
from unittest.mock import patch

from review_callback import build_payload, callback_secret, payload_batches, push_review_assessments
from sunlight_v2 import METHOD_VERSION


def record(disposition="selected_possible"):
    return {
        "candidateId": "abc123", "disposition": disposition, "decisionStage": "final_selection",
        "features": {
            "observer": {"lat": 39.29, "lon": -76.61},
            "rain": {"lat": 39.3, "lon": -76.3, "antiSolarRainArcSpanDeg": 35},
            "geometry": {"sunElevationDeg": 8.5, "antiSolarBearingDeg": 105},
            "thresholdSnapshot": {"directWatchMinWm2": 120},
            "sunlightV2": {
                "methodVersion": METHOD_VERSION, "assessmentProcessingAt": "2026-07-28T23:36:00Z",
                "enrichmentLatencySeconds": 120, "sunlightState": "sunlight_plausible",
                "sunlightStateReasons": ["band2_gap_in_one_or_weakly_correlated_frame"],
                "band2": {"normalizationVersion": "b2-v1", "temporalFramesUsed": 2, "sourceKeys": ["key"]},
                "acmc": {"sourceKey": "acmc", "createdAt": "2026-07-28T23:35:00Z"},
                "dsrf": {"sourceKey": "dsrf", "unavailableReason": "outside_range"},
                "metar": {"methodVersion": "metar-v1", "stations": [{"stationId": "KBWI", "observedAt": "2026-07-28T23:30:00Z"}]},
            },
        },
    }


class FakeResponse:
    ok = True
    status_code = 200
    text = ""
    def json(self): return {"attached": 1}


class FakeSession:
    def __init__(self): self.calls = []
    def post(self, *args, **kwargs): self.calls.append((args, kwargs)); return FakeResponse()


class FakeSsm:
    def get_parameter(self, **kwargs):
        self.kwargs = kwargs
        return {"Parameter": {"Value": "secure-value"}}


class ReviewCallbackTests(unittest.TestCase):
    def test_payload_includes_selected_and_excludes_rejected(self):
        envelope = {"detectorRuleVersion": "rule-v1", "radar": {"observedAt": "2026-07-28T23:34:00Z", "rainFootprintId": "fp", "rainFootprintContentSha256": "hash"}, "records": [record(), record("rejected")]}
        payload = build_payload(envelope)
        self.assertEqual(len(payload["assessments"]), 1)
        item = payload["assessments"][0]
        self.assertEqual(len(item["idempotencyKey"]), 64)
        self.assertEqual(item["rainFootprint"]["contentSha256"], "hash")
        self.assertEqual(item["sources"]["metar"]["stations"][0]["stationId"], "KBWI")

    def test_payload_includes_review_only_geometry_selection(self):
        item = record("selected_research_possible")
        item["features"]["researchReview"] = {"ruleVersion": "geometry-first-review-2026-07-v1", "reviewOnly": True}
        payload = build_payload({"radar": {"observedAt": "2026-07-28T23:34:00Z"}, "records": [item]})
        self.assertEqual(payload["assessments"][0]["researchReview"]["reviewOnly"], True)

    def test_secret_comes_from_secure_parameter(self):
        fake = FakeSsm()
        with patch.dict(os.environ, {"REVIEW_ENRICH_SECRET_PARAMETER": "/test/secret"}, clear=True):
            self.assertEqual(callback_secret(fake), "secure-value")
        self.assertTrue(fake.kwargs["WithDecryption"])

    def test_callback_is_compact_and_signed(self):
        session = FakeSession()
        envelope = {"detectorRuleVersion": "rule-v1", "radar": {"observedAt": "2026-07-28T23:34:00Z"}, "records": [record()]}
        with patch.dict(os.environ, {"RAINBOW_REVIEW_ENRICH_URL": "https://example.test/review"}, clear=True):
            result = push_review_assessments(envelope, session=session, secret="secret")
        self.assertTrue(result["ok"])
        args, kwargs = session.calls[0]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertLess(len(kwargs["data"]), 128 * 1024)
        self.assertEqual(json.loads(kwargs["data"])["schemaVersion"], "review-assessment.v1")

    def test_missing_callback_url_skips_before_reading_secret(self):
        envelope = {"radar": {"observedAt": "2026-07-28T23:34:00Z"}, "records": [record()]}
        with patch.dict(os.environ, {"REVIEW_ENRICH_SECRET_PARAMETER": "/test/secret"}, clear=True), \
             patch("review_callback.callback_secret", side_effect=AssertionError("secret should not be read")):
            result = push_review_assessments(envelope)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "review callback not configured")

    def test_large_payload_is_split_below_the_http_cap(self):
        envelope = {"detectorRuleVersion": "rule-v1", "radar": {"observedAt": "2026-07-28T23:34:00Z"}, "records": []}
        for index in range(80):
            item = record()
            item["candidateId"] = f"candidate-{index}"
            item["features"]["sunlightV2"]["sunlightStateReasons"] = ["x" * 3000]
            envelope["records"].append(item)
        batches = payload_batches(build_payload(envelope))
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(body) <= 128 * 1024 for _, body in batches))
        self.assertTrue(all(len(batch["assessments"]) <= 32 for batch, _ in batches))


if __name__ == "__main__":
    unittest.main()
