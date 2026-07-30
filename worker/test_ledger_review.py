import unittest
from datetime import datetime, timezone

import numpy as np

from ledger_review import MAX_PER_SCAN, select_ledger_review_records
from opportunity_ledger import build_opportunity_ledger
from rain_footprint import build_sidecar


def example():
    lats = np.array([41.02, 41.01, 41.00])
    lons = np.array([-94.02, -94.01, -94.00])
    rates = np.zeros((3, 3)); rates[1, 1] = 1.5
    sidecar = build_sidecar(lats, lons, rates, datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc), "synthetic-iowa")
    previous = build_opportunity_ledger(sidecar)
    return sidecar, previous, build_opportunity_ledger(sidecar, previous=previous)


class LedgerReviewTests(unittest.TestCase):
    def test_requires_persistence_and_camera_geometry_and_caps_at_two(self):
        sidecar, previous, ledger = example()
        opportunity = ledger["opportunities"][0]
        first = opportunity["observerSwath"]["representativeCandidates"][0]
        catalog = [{"id": 1, "name": "Test airport", "lat": first["lat"], "lon": first["lon"],
                    "cameras": [{"id": 10, "bearing": 0, "direction": "North", "mapWedgeAngle": 360}]}]
        result = select_ledger_review_records(ledger, previous, sidecar, catalog)
        self.assertLessEqual(result["selected"], MAX_PER_SCAN)
        self.assertEqual(result["selected"], 1)
        record = result["records"][0]
        self.assertEqual(record["features"]["researchReview"]["source"], "opportunity_ledger")
        self.assertEqual(record["features"]["researchReview"]["currentDetectorDisposition"], "not_generated_as_detector_candidate")
        self.assertTrue(record["features"]["rain"]["antiSolarRainSpanByTier"])

    def test_first_scan_never_enters_review(self):
        sidecar, _, ledger = example()
        self.assertEqual(select_ledger_review_records(ledger, None, sidecar, [])["selected"], 0)


if __name__ == "__main__":
    unittest.main()
