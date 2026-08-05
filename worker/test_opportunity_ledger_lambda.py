import gzip
import hashlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import opportunity_ledger_lambda as target


def empty_v4_selection():
    return {"ruleVersion": "v4-shadow-review-2026-08-v1", "modelVersion": "model",
            "eligibleFamilies": 0, "primarySelected": 0, "controlSelected": 0,
            "predictionManifestSha256": "a" * 64, "predictions": [], "selected": 0,
            "selectedCandidateIds": [], "unattachedSeeds": 0, "records": [],
            "state": {"stormLedger": {"stormObjects": []}}}


def empty_v5_selection():
    return {"schemaVersion": "v5-candidate-scan.v1", "scanTime": "2026-07-30T23:30:00Z",
            "ruleVersion": "v5", "modelVersion": "model", "predictionManifestSha256": "b" * 64,
            "predictions": [], "records": [], "retained": 0, "overlap": 0, "expandedOnly": 0,
            "upstreamAudit": {}, "acquisitionEnabled": False, "state": {}}


class Body:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class FakeS3:
    def __init__(self, compressed):
        self.compressed = compressed

    def get_object(self, **_kwargs):
        return {"Body": Body(self.compressed)}


class OpportunityLedgerLambdaTests(unittest.TestCase):
    def test_disabled_review_lane_still_persists_private_ledger(self):
        sidecar = {"observedAt": "2026-07-30T23:30:00Z"}
        raw = json.dumps(sidecar).encode("utf-8")
        fake_s3 = FakeS3(gzip.compress(raw))
        ledger = {
            "scanTime": sidecar["observedAt"], "rainFootprintId": "fp-1",
            "stats": {"rainEvents": 1, "opportunities": 1, "observerSwathCells": 10},
        }
        with patch.dict(sys.modules, {"boto3": SimpleNamespace(client=lambda _name: fake_s3)}), \
             patch.dict(os.environ, {"RAINBOW_RESEARCH_BUCKET": "research"}, clear=True), \
             patch.object(target, "load_previous", return_value=(None, None)), \
             patch.object(target, "build_opportunity_ledger", return_value=ledger), \
             patch.object(target, "persist", return_value={"key": "stored"}), \
             patch.object(target, "load_faa_catalog", return_value=[]), \
             patch.object(target, "select_v4_shadow_records", return_value=empty_v4_selection()), \
             patch.object(target, "score_v5_shadow_records", return_value=empty_v5_selection()), \
             patch.object(target, "safe_push_v5_review_feed", return_value={"ok": True, "items": 0}), \
             patch.object(target, "safe_push_review_assessments", side_effect=AssertionError("callback must not run")):
            result = target.handler({
                "bucket": "source", "key": "footprint",
                "contentSha256": hashlib.sha256(raw).hexdigest(),
            }, None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["storage"]["key"], "stored")
        self.assertTrue(result["reviewSelection"]["skipped"])
        self.assertEqual(result["reviewCallback"]["reason"], "ledger review lane disabled")

    def test_enabled_v4_lane_does_not_require_a_catalog_camera(self):
        sidecar = {"observedAt": "2026-07-30T23:30:00Z"}
        raw = json.dumps(sidecar).encode("utf-8")
        fake_s3 = FakeS3(gzip.compress(raw))
        ledger = {"scanTime": sidecar["observedAt"], "rainFootprintId": "fp-1",
                  "stats": {"rainEvents": 1, "opportunities": 1, "observerSwathCells": 10}}
        persisted = {"called": False}
        def persist(*_args, **_kwargs):
            persisted["called"] = True
            return {"key": "stored"}
        with patch.dict(sys.modules, {"boto3": SimpleNamespace(client=lambda _name: fake_s3)}), \
             patch.dict(os.environ, {"RAINBOW_RESEARCH_BUCKET": "research",
                                     "RAINBOW_REVIEW_ENRICH_URL": "https://example.test/review"}, clear=True), \
             patch.object(target, "load_previous", return_value=(None, None)), \
             patch.object(target, "build_opportunity_ledger", return_value=ledger), \
             patch.object(target, "persist", side_effect=persist), \
             patch.object(target, "load_faa_catalog", return_value=[]), \
             patch.object(target, "select_v4_shadow_records", return_value=empty_v4_selection()), \
             patch.object(target, "score_v5_shadow_records", return_value=empty_v5_selection()), \
             patch.object(target, "safe_push_v5_review_feed", return_value={"ok": True, "items": 0}), \
             patch.object(target, "safe_push_review_assessments", return_value={"ok": True, "assessments": 0}):
            result = target.handler({"bucket": "source", "key": "footprint",
                                     "contentSha256": hashlib.sha256(raw).hexdigest()}, None)
        self.assertTrue(persisted["called"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reviewSelection"]["selected"], 0)

    def test_source_hash_mismatch_fails_before_build(self):
        raw = json.dumps({"observedAt": "2026-07-30T23:30:00Z"}).encode("utf-8")
        fake_s3 = FakeS3(gzip.compress(raw))
        with patch.dict(sys.modules, {"boto3": SimpleNamespace(client=lambda _name: fake_s3)}):
            with self.assertRaisesRegex(ValueError, "content hash mismatch"):
                target.handler({"bucket": "source", "key": "footprint", "contentSha256": "wrong"}, None)


if __name__ == "__main__":
    unittest.main()
