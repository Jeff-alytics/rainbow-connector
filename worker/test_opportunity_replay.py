import hashlib
import json
import unittest
from pathlib import Path

from opportunity_ledger import encode_swath
from opportunity_replay import build_report


class OpportunityReplayTests(unittest.TestCase):
    EXPECTED_OBSERVATIONS = {
        "baltimore-dundalk-20260728": {"baltimore": (39.2904, -76.6122), "dundalk": (39.2507, -76.5205)},
        "colorado-multicamera-20260730": {"silver-west": (38.02381, -105.37973), "saguache-ne": (38.09902, -106.17035), "fremont-county": (38.4323, -105.1031)},
        "utah-ogden-bear-river-20260730": {"ogden": (41.193604, -112.00825), "bear-river": (41.724174, -112.18263)},
        "middletown-connecticut-20260729": {"middletown": (41.5623, -72.6506)},
        "meeker-camera-20260709": {"meeker": (40.044106, -107.88861)},
        "delano-low-sun-20260729": {"delano": (41.723, -114.1839)},
    }

    def test_frozen_case_fixture_hash_and_unresolved_time_are_explicit(self):
        path = Path(__file__).resolve().parents[1] / "validation" / "opportunity-ledger" / "case-fixture-v1.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        expected = fixture.pop("contentSha256")
        canonical = json.dumps(fixture, separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), expected)
        middletown = next(item for item in fixture["cases"] if item["id"] == "middletown-connecticut-20260729")
        self.assertEqual(middletown["status"], "replay_ready")
        self.assertEqual(middletown["precision"], "bounded_before_tweet_post")
        self.assertEqual(middletown["window"][1], "2026-07-29T22:25:00Z")

    def test_report_separates_swath_from_representative_placement(self):
        swath = {
            "runs": encode_swath({(10, 10), (10, 11)}),
            "representativeCandidates": [{"lat": 24.25, "lon": -124.75}],
        }
        ledger = {
            "scanTime": "2026-07-30T02:25:00Z", "methodVersion": "test",
            "rainEvents": [{"eventId": "storm"}],
            "opportunities": [{"eventId": "storm", "observerSwath": swath}],
        }
        fixture = {
            "contentSha256": "fixture", "cases": [{
                "id": "case", "status": "replay_ready",
                "window": ["2026-07-30T02:20:00Z", "2026-07-30T02:30:00Z"],
                "observations": [{"id": "observer", "lat": 24.25, "lon": -124.725, "label": "rainbow"}],
            }],
        }
        report = build_report(fixture, [ledger])
        row = report["cases"][0]
        self.assertEqual(row["disposition"], "evaluated")
        self.assertTrue(row["rainEventRetained"])
        self.assertTrue(row["observations"][0]["insideObserverSwath"])
        self.assertEqual(row["observations"][0]["matchedEventIds"], ["storm"])

    def test_missing_time_and_missing_ledgers_are_explicit(self):
        fixture = {"cases": [
            {"id": "unknown-time", "status": "awaiting_time_recovery", "window": [None, None], "observations": []},
            {"id": "missing", "status": "replay_ready", "window": ["2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"], "observations": []},
        ]}
        report = build_report(fixture, [])
        self.assertEqual([item["disposition"] for item in report["cases"]], ["fixture_not_ready", "ledger_window_missing"])

    def test_frozen_report_pins_every_named_case_and_observer_location(self):
        root = Path(__file__).resolve().parents[1] / "validation" / "opportunity-ledger"
        fixture = json.loads((root / "case-fixture-v1.json").read_text(encoding="utf-8"))
        report = json.loads((root / "replay-report-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(report["fixtureContentSha256"], fixture["contentSha256"])
        self.assertEqual(set(self.EXPECTED_OBSERVATIONS), {item["caseId"] for item in report["cases"]})
        self.assertEqual(report["summary"]["cases"], 6)
        self.assertEqual(report["summary"]["evaluated"], 6)
        self.assertEqual(report["summary"]["observationsInsideSwath"], 10)
        for case in report["cases"]:
            self.assertEqual(case["disposition"], "evaluated")
            self.assertTrue(case["rainEventRetained"])
            self.assertTrue(case["opportunityRetained"])
            expected = self.EXPECTED_OBSERVATIONS[case["caseId"]]
            self.assertEqual(set(expected), {item["observationId"] for item in case["observations"]})
            for observation in case["observations"]:
                self.assertEqual(
                    (observation["observer"]["lat"], observation["observer"]["lon"]),
                    expected[observation["observationId"]],
                )
                self.assertTrue(observation["insideObserverSwath"])
                self.assertLessEqual(observation["nearestSwathKm"], 3)
                self.assertEqual(observation["failureCodes"], [])


if __name__ == "__main__":
    unittest.main()
