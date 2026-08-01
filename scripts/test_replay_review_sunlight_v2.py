import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


replay = load_script("replay_review_sunlight_v2", "replay-review-sunlight-v2.py")
backfill = load_script("backfill_review_sunlight_v2", "backfill-review-sunlight-v2.py")


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, results, events):
        self.responses = iter([Response(results), Response(events)])
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return next(self.responses)


class ReplaySafetyTests(unittest.TestCase):
    start = datetime(2026, 7, 31, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def test_redeliver_requires_apply(self):
        with self.assertRaises(SystemExit):
            replay.validate_flags(False, True)
        replay.validate_flags(True, True)
        replay.validate_flags(False, False)

    def test_queries_are_date_bounded(self):
        url = replay.event_query_url("https://example.test", True, self.start, self.end)
        self.assertIn("results=1", url)
        self.assertIn("since=2026-07-31T00%3A00%3A00Z", url)
        self.assertIn("until=2026-08-01T00%3A00%3A00Z", url)
        self.assertIn("limit=1000", url)
        self.assertIn("since=", backfill.event_query_url("https://example.test", False, self.start, self.end))

    def test_withheld_results_are_not_reported_as_missing(self):
        results = {
            "events": 2,
            "items": [
                {"id": "withheld", "reviewedAt": "2026-07-31T12:00:00Z",
                 "sunlightAssessment": None, "sunlightAssessmentWithheld": True},
                {"id": "missing", "reviewedAt": "2026-07-31T12:00:00Z",
                 "sunlightAssessment": None, "sunlightAssessmentWithheld": False},
            ],
        }
        events = {"events": 2, "items": [{"id": "withheld"}, {"id": "missing"}]}
        session = Session(results, events)
        found = replay.missing_events(session, "https://example.test", self.start, self.end)
        self.assertEqual([event["id"] for event in found], ["missing"])

    def test_replay_requires_recorded_rain_point(self):
        event = {
            "id": "event-1",
            "firstSeenAt": "2026-07-31T12:00:00Z",
            "representative": {
                "detectedAt": "2026-07-31T12:00:00Z",
                "lat": 40.0, "lon": -75.0, "score": 80,
                "direction": {"bearing": 180},
                "evidence": {"sunElevationDeg": 8.0},
            },
        }
        self.assertIsNone(replay.replay_record(event, "2026-07-31T12:00:00Z"))
        event["representative"]["evidence"]["rainPoint"] = {
            "lat": 40.1, "lon": -75.0, "distanceKm": 12.5,
        }
        record = replay.replay_record(event, "2026-07-31T12:00:00Z")
        self.assertEqual(record["features"]["rain"]["lat"], 40.1)
        self.assertEqual(record["features"]["rain"]["distanceKm"], 12.5)


if __name__ == "__main__":
    unittest.main()
