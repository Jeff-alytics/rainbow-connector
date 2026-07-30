import json
import unittest

from shadow_dispatch import invoke_sunlight_v2, safe_invoke_sunlight_v2
from shadow_lambda import already_enriched, candidate_from_record


class FakeLambda:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def invoke(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)
        return {"StatusCode": 202}


class ShadowDispatchTests(unittest.TestCase):
    def test_async_dispatch_sends_only_private_log_reference(self):
        client = FakeLambda()
        result = invoke_sunlight_v2({"ok": True, "bucket": "private", "key": "decision-log/rolling/v1/item.json.gz"}, 12, function_name="shadow-worker", lambda_client=client)
        self.assertTrue(result["accepted"])
        call = client.calls[0]
        self.assertEqual(call["InvocationType"], "Event")
        self.assertEqual(json.loads(call["Payload"]), {"bucket": "private", "key": "decision-log/rolling/v1/item.json.gz"})

    def test_empty_scan_never_invokes_shadow(self):
        client = FakeLambda()
        result = invoke_sunlight_v2({"ok": True, "bucket": "private", "key": "x"}, 0, function_name="shadow", lambda_client=client)
        self.assertTrue(result["skipped"])
        self.assertEqual(client.calls, [])

    def test_dispatch_failure_is_non_operational(self):
        result = safe_invoke_sunlight_v2({"ok": True, "bucket": "private", "key": "decision-log/rolling/x"}, 1, function_name="shadow", lambda_client=FakeLambda(RuntimeError("invoke failed")))
        self.assertFalse(result["ok"])
        self.assertFalse(result["operationalImpact"])

    def test_shadow_reconstructs_candidate_from_replayable_features(self):
        record = {"features": {
            "observer": {"lat": 39.29, "lon": -76.61},
            "rain": {"lat": 39.29, "lon": -76.3, "distanceKm": 25, "rateMmHr": 1, "observerRateMmHr": 0},
            "geometry": {"sunElevationDeg": 8.5, "sunBearingDeg": 285, "antiSolarBearingDeg": 105, "radarScore": 94},
        }}
        candidate = candidate_from_record(record)
        self.assertEqual(candidate["antiSolarBearingDeg"], 105)
        self.assertEqual(candidate["rainDistanceKm"], 25)

    def test_completed_current_method_is_immutable(self):
        self.assertTrue(already_enriched({"shadowV2": {
            "methodVersion": "sunlight-v2-shadow-2026-07-v3", "status": "complete",
        }}))
        self.assertTrue(already_enriched({"shadowV2": {
            "methodVersion": "sunlight-v2-shadow-2026-07-v3", "status": "complete_with_errors",
        }}))
        self.assertFalse(already_enriched({"shadowV2": {
            "methodVersion": "sunlight-v2-shadow-2026-07-v1", "status": "complete",
        }}))
        self.assertFalse(already_enriched({"shadowV2": {
            "methodVersion": "sunlight-v2-shadow-2026-07-v3", "status": "pending",
        }}))


if __name__ == "__main__":
    unittest.main()
