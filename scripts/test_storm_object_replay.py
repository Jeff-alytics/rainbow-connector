import unittest
from datetime import datetime, timedelta, timezone

from scripts.storm_object_replay import TokenScheduler


def candidate(event_id="family-a", score=90):
    return {
        "eventId": event_id,
        "ancestorEventIds": [event_id],
        "score": score,
        "causalPeakRadar": 99,
        "objectPersistenceScans": 3,
        "eligible": True,
    }


class TokenSchedulerTests(unittest.TestCase):
    def test_refills_without_clock_reset_and_uses_token_time(self):
        scheduler = TokenScheduler(3, 80)
        start = datetime(2026, 7, 3, 23, 50, tzinfo=timezone.utc)
        self.assertEqual(len(scheduler.grant(start, [
            candidate("a"), candidate("b"), candidate("c"), candidate("d")
        ])), 3)
        self.assertEqual(scheduler.grant(start + timedelta(minutes=10), [candidate("d")]), [])
        # Crossing 00Z does not reset the bucket.
        self.assertEqual(scheduler.grant(start + timedelta(minutes=20), [candidate("d")]), [])
        self.assertEqual(
            len(scheduler.grant(start + timedelta(hours=2, minutes=30), [candidate("d")])),
            1,
        )

    def test_merge_inherits_latest_ancestor_cooldown_but_can_realert(self):
        scheduler = TokenScheduler(3, 80)
        start = datetime(2026, 7, 3, tzinfo=timezone.utc)
        scheduler.grant(start, [candidate("a")])
        merged = candidate("merged")
        merged["ancestorEventIds"] = ["a", "b"]
        self.assertEqual(scheduler.grant(start + timedelta(hours=11), [merged]), [])
        alerts = scheduler.grant(start + timedelta(hours=12), [merged])
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0]["isRealertAfterCooldown"])

    def test_threshold_and_deterministic_rank_control_token_grants(self):
        scheduler = TokenScheduler(2, 90)
        now = datetime(2026, 7, 3, tzinfo=timezone.utc)
        alerts = scheduler.grant(now, [
            candidate("low", 89),
            candidate("second", 95),
            candidate("first", 99),
            candidate("third", 94),
        ])
        self.assertEqual([item["eventId"] for item in alerts], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
