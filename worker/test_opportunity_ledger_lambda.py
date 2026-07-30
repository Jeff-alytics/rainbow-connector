import gzip
import hashlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import opportunity_ledger_lambda as target


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
    def test_review_selection_failure_never_fails_private_ledger_storage(self):
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
             patch.object(target, "safe_push_review_assessments", side_effect=AssertionError("callback must not run")):
            result = target.handler({
                "bucket": "source", "key": "footprint",
                "contentSha256": hashlib.sha256(raw).hexdigest(),
            }, None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["storage"]["key"], "stored")
        self.assertFalse(result["reviewSelection"]["ok"])
        self.assertEqual(result["reviewCallback"]["reason"], "selection_failed")

    def test_source_hash_mismatch_fails_before_build(self):
        raw = json.dumps({"observedAt": "2026-07-30T23:30:00Z"}).encode("utf-8")
        fake_s3 = FakeS3(gzip.compress(raw))
        with patch.dict(sys.modules, {"boto3": SimpleNamespace(client=lambda _name: fake_s3)}):
            with self.assertRaisesRegex(ValueError, "content hash mismatch"):
                target.handler({"bucket": "source", "key": "footprint", "contentSha256": "wrong"}, None)


if __name__ == "__main__":
    unittest.main()
