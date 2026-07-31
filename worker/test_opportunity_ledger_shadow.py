import io
import json
import sys
import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

# shadow_lambda reaches sunlight_v2 -> goes_sample, which lives in api/. Without
# this guard the module only imports when an alphabetically earlier test module
# has already patched sys.path, so running this file alone would fail.
API_DIR = Path(__file__).resolve().parents[1] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from opportunity_ledger_dispatch import invoke_opportunity_ledger, safe_invoke_opportunity_ledger
from opportunity_ledger_store import encode, load_previous, object_key
from shadow_lambda import delivery_summary


class FakeLambda:
    def __init__(self): self.call = None
    def invoke(self, **kwargs): self.call = kwargs; return {"StatusCode": 202}


class FakeS3:
    def __init__(self, objects): self.objects = objects
    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(kwargs["Prefix"])]}
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.objects[Key])}


class CloudFormationLoader(yaml.SafeLoader):
    pass


def cloudformation_tag(loader, tag_suffix, node):
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, SequenceNode):
        value = loader.construct_sequence(node)
    elif isinstance(node, MappingNode):
        value = loader.construct_mapping(node)
    else:
        raise TypeError(f"Unsupported YAML node: {type(node).__name__}")
    return {tag_suffix: value}


CloudFormationLoader.add_multi_constructor("!", cloudformation_tag)


class OpportunityLedgerShadowTests(unittest.TestCase):
    def test_infrastructure_is_private_rolling_and_post_publication(self):
        root = Path(__file__).resolve().parents[1]
        template_text = (root / "template.yaml").read_text(encoding="utf-8")
        template = yaml.load(template_text, Loader=CloudFormationLoader)
        source = (root / "worker" / "lambda_function.py").read_text(encoding="utf-8")
        resources = template["Resources"]
        shadow = resources["SunlightShadowWorker"]["Properties"]
        ledger = resources["OpportunityLedgerWorker"]["Properties"]
        shadow_env = shadow["Environment"]["Variables"]
        ledger_env = ledger["Environment"]["Variables"]
        self.assertEqual(shadow["ImageConfig"]["Command"], ["worker.shadow_lambda.handler"])
        self.assertEqual(ledger["ImageConfig"]["Command"], ["worker.opportunity_ledger_lambda.handler"])
        self.assertEqual(shadow_env["RAINBOW_REVIEW_ENRICH_URL"], {"Ref": "ReviewEnrichUrl"})
        self.assertEqual(shadow_env["RAINBOW_RESEARCH_REVIEW_ENABLED"], "false")
        self.assertNotIn("RAINBOW_REVIEW_ENRICH_URL", ledger_env)
        self.assertNotIn("RAINBOW_PUBLISH_URL", ledger_env)
        lifecycle = resources["RainbowResearchBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
        self.assertTrue(any(rule.get("Prefix") == "opportunity-ledger/rolling/" for rule in lifecycle))
        invoke = ledger["EventInvokeConfig"]
        self.assertEqual(invoke["MaximumEventAgeInSeconds"], 120)
        self.assertEqual(invoke["MaximumRetryAttempts"], 0)
        topic = resources["OpportunityLedgerAlarmTopic"]
        self.assertEqual(topic["Type"], "AWS::SNS::Topic")
        for function_name in ("MapTierWorker", "SunlightShadowWorker", "OpportunityLedgerWorker"):
            event_invoke = resources[function_name]["Properties"]["EventInvokeConfig"]
            self.assertEqual(
                event_invoke["DestinationConfig"]["OnFailure"],
                {"Type": "SNS", "Destination": {"Ref": "OpportunityLedgerAlarmTopic"}},
            )
        alarm_specs = {
            "RainbowWorker": ("RainbowWorkerDurationAlarm", "RainbowWorkerErrorsAlarm", "RainbowWorkerStaleAlarm"),
            "MapTierWorker": ("MapTierWorkerDurationAlarm", "MapTierWorkerErrorsAlarm", "MapTierWorkerStaleAlarm"),
            "SunlightShadowWorker": ("SunlightShadowWorkerDurationAlarm", "SunlightShadowWorkerErrorsAlarm", "SunlightShadowWorkerStaleAlarm"),
            "OpportunityLedgerWorker": ("OpportunityLedgerDurationAlarm", "OpportunityLedgerErrorsAlarm", "OpportunityLedgerStaleAlarm"),
        }
        for function_name, names in alarm_specs.items():
            duration = resources[names[0]]["Properties"]
            errors = resources[names[1]]["Properties"]
            stale = resources[names[2]]["Properties"]
            expected_dimension = [{"Name": "FunctionName", "Value": {"Ref": function_name}}]
            self.assertEqual(duration["MetricName"], "Duration")
            self.assertEqual(errors["MetricName"], "Errors")
            self.assertEqual(stale["MetricName"], "Invocations")
            self.assertEqual(duration["Dimensions"], expected_dimension)
            self.assertEqual(errors["Dimensions"], expected_dimension)
            self.assertEqual(stale["Dimensions"], expected_dimension)
            self.assertEqual(duration["TreatMissingData"], "notBreaching")
            self.assertEqual(errors["TreatMissingData"], "notBreaching")
            self.assertEqual(stale["TreatMissingData"], "breaching")
            self.assertEqual(duration["EvaluationPeriods"], 2)
            self.assertEqual(errors["EvaluationPeriods"], 2)
            self.assertGreaterEqual(stale["Period"], 900)
            self.assertEqual(stale["ComparisonOperator"], "LessThanThreshold")
            for name in names:
                self.assertEqual(resources[name]["Properties"]["AlarmActions"], [{"Ref": "OpportunityLedgerAlarmTopic"}])
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

    def test_previous_scan_older_than_lineage_window_is_rejected(self):
        ledger = {"scanTime": "2026-07-30T01:30:00Z", "rainFootprintId": "fp"}
        body, _, _ = encode(ledger)
        key = object_key(ledger)
        previous, previous_key = load_previous("private", "2026-07-30T01:46:00Z", FakeS3({key: body}))
        self.assertIsNone(previous)
        self.assertIsNone(previous_key)

    def test_delivery_summary_reports_counts_skips_and_failures(self):
        # A healthy lane must be visible in logs, because a pending review item
        # withholds its own assessment and shows nothing in the workbench.
        delivered = delivery_summary({"ok": True, "assessments": 7,
                                      "response": {"received": 7, "attached": 3, "duplicates": 0, "unmatched": 4}})
        self.assertEqual(delivered["ok"], True)
        self.assertEqual(delivered["assessments"], 7)
        self.assertEqual(delivered["attached"], 3)
        self.assertEqual(delivered["unmatched"], 4)
        self.assertNotIn("skipped", delivered)
        # A silent skip is the failure mode that hid the outage; it must surface.
        skipped = delivery_summary({"ok": True, "skipped": True, "assessments": 0,
                                    "reason": "review callback not configured"})
        self.assertEqual(skipped["skipped"], "review callback not configured")
        self.assertEqual(skipped["assessments"], 0)
        failed = delivery_summary({"ok": False, "error": "Review assessment callback failed (401): {}"})
        self.assertEqual(failed["ok"], False)
        self.assertIn("401", failed["error"])
        # Must be JSON-serialisable so the log line stays machine-readable.
        self.assertEqual(json.loads(json.dumps(delivered))["attached"], 3)


if __name__ == "__main__": unittest.main()
