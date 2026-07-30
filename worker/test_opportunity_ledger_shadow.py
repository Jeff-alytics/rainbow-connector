import io
import json
import unittest
from pathlib import Path

from opportunity_ledger_dispatch import invoke_opportunity_ledger, safe_invoke_opportunity_ledger
from opportunity_ledger_store import encode, load_previous, object_key


class FakeLambda:
    def __init__(self): self.call = None
    def invoke(self, **kwargs): self.call = kwargs; return {"StatusCode": 202}


class FakeS3:
    def __init__(self, objects): self.objects = objects
    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(kwargs["Prefix"])]}
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.objects[Key])}


class OpportunityLedgerShadowTests(unittest.TestCase):
    def test_infrastructure_is_private_rolling_and_post_publication(self):
        root = Path(__file__).resolve().parents[1]
        template = (root / "template.yaml").read_text(encoding="utf-8")
        source = (root / "worker" / "lambda_function.py").read_text(encoding="utf-8")
        self.assertIn("OpportunityLedgerWorker:", template)
        self.assertIn("worker.opportunity_ledger_lambda.handler", template)
        self.assertIn("Prefix: opportunity-ledger/rolling/", template)
        self.assertIn("MaximumEventAgeInSeconds: 300", template)
        self.assertIn("MaximumRetryAttempts: 0", template)
        self.assertIn("ReadWriteRollingOpportunityLedgers", template)
        self.assertNotIn("RAINBOW_PUBLISH_URL: !Ref PublishUrl", template.split("OpportunityLedgerWorker:", 1)[1])
        self.assertLess(source.index("stored = publish_artifact"), source.index("notified = notify_subscribers"))
        self.assertLess(source.index("notified = notify_subscribers"), source.index("rain_footprint ="))
        self.assertLess(source.index("rain_footprint ="), source.index("opportunity_ledger ="))

    def test_dispatch_is_async_and_hash_pinned(self):
        client = FakeLambda()
        result = invoke_opportunity_ledger({"ok": True, "bucket": "private", "key": "footprint", "contentSha256": "hash"}, function_name="ledger", lambda_client=client)
        self.assertTrue(result["accepted"])
        self.assertEqual(client.call["InvocationType"], "Event")
        self.assertEqual(json.loads(client.call["Payload"])["contentSha256"], "hash")

    def test_dispatch_failure_never_claims_operational_impact(self):
        class Broken:
            def invoke(self, **kwargs): raise RuntimeError("nope")
        result = safe_invoke_opportunity_ledger({"ok": True, "bucket": "b", "key": "k"}, function_name="ledger", lambda_client=Broken())
        self.assertFalse(result["ok"])
        self.assertFalse(result["operationalImpact"])

    def test_storage_is_deterministic_and_previous_scan_is_loaded(self):
        ledger = {"scanTime": "2026-07-30T01:30:00Z", "rainFootprintId": "fp"}
        first, first_hash, _ = encode(ledger); second, second_hash, _ = encode(ledger)
        self.assertEqual(first, second); self.assertEqual(first_hash, second_hash)
        key = object_key(ledger)
        s3 = FakeS3({key: first})
        previous, previous_key = load_previous("private", "2026-07-30T01:40:00Z", s3)
        self.assertEqual(previous["rainFootprintId"], "fp")
        self.assertEqual(previous_key, key)


if __name__ == "__main__": unittest.main()
