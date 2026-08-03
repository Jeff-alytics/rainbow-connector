import gzip
import json
import unittest
from unittest.mock import patch

from decision_store import SCHEMA_VERSION, build_envelope, object_key, persist_decision_log, safe_persist_decision_log
import lambda_function


ARTIFACT = {
    "generatedAt": "2026-07-28T23:04:31Z",
    "sourceHealth": {"radar": {
        "provider": "NOAA MRMS", "observedAt": "2026-07-28T23:02:00Z",
        "s3Key": "CONUS/PrecipRate_00.00/example.grib2.gz",
    }},
    "candidates": [], "possibleCandidates": [],
}


class FakeS3:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def put_object(self, **kwargs):
        if self.error:
            raise self.error
        self.calls.append(kwargs)


class DecisionStoreTests(unittest.TestCase):
    def test_envelope_and_key_are_schema_versioned(self):
        artifact = {**ARTIFACT, "sourceHealth": {"radar": {**ARTIFACT["sourceHealth"]["radar"], "rainFootprintId": "mrms-footprint-20260728T230200Z"}}}
        envelope = build_envelope(artifact, [{"disposition": "rejected"}])
        self.assertEqual(envelope["schemaVersion"], SCHEMA_VERSION)
        self.assertEqual(envelope["scanId"], "mrms-20260728T230200Z")
        self.assertEqual(envelope["metrics"]["v1V2DisagreementRateByDisposition"]["rejected"]["missing"], 1)
        self.assertEqual(envelope["radar"]["rainFootprintId"], "mrms-footprint-20260728T230200Z")
        self.assertEqual(
            object_key(envelope),
            "decision-log/rolling/candidate-decision-log.v1/2026/07/28/mrms-20260728T230200Z.json.gz",
        )

    def test_s3_payload_is_compressed_hashed_and_private(self):
        s3 = FakeS3()
        result = persist_decision_log(ARTIFACT, [{"disposition": "rejected"}], bucket="private-bucket", s3_client=s3)
        self.assertTrue(result["ok"])
        call = s3.calls[0]
        self.assertEqual(call["Bucket"], "private-bucket")
        self.assertEqual(call["ServerSideEncryption"], "AES256")
        self.assertEqual(call["Metadata"]["schema-version"], SCHEMA_VERSION)
        body = json.loads(gzip.decompress(call["Body"]))
        self.assertEqual(body["records"][0]["disposition"], "rejected")

    def test_storage_failure_is_returned_not_raised(self):
        result = safe_persist_decision_log(
            ARTIFACT, [{"disposition": "rejected"}],
            bucket="private-bucket", s3_client=FakeS3(RuntimeError("write failed")),
        )
        self.assertFalse(result["ok"])
        self.assertIn("write failed", result["error"])

    @patch.object(lambda_function, "safe_build_and_persist_rain_footprint", return_value={"ok": False, "error": "sidecar failed"})
    @patch.object(lambda_function, "safe_persist_decision_log", return_value={"ok": False, "error": "write failed"})
    @patch.object(lambda_function, "notify_subscribers", return_value={"ok": True, "sent": 1})
    @patch.object(lambda_function, "publish_artifact", return_value={"ok": True})
    @patch.object(lambda_function, "build_final_artifact", return_value=ARTIFACT)
    def test_log_failure_cannot_block_publication_or_email(self, build, publish, notify, research, footprint):
        result = lambda_function.handler({}, None)
        self.assertTrue(result["ok"])
        publish.assert_called_once()
        notify.assert_called_once()
        research.assert_called_once()
        self.assertEqual(result["notifications"]["sent"], 1)
        self.assertFalse(result["decisionLog"]["ok"])
        self.assertFalse(result["rainFootprint"]["ok"])


if __name__ == "__main__":
    unittest.main()
