import unittest
from datetime import datetime, timezone

import numpy as np

from detector_core import solar_position
from ledger_review import MAX_PER_SCAN, load_faa_catalog, select_ledger_review_records
from opportunity_ledger import build_opportunity_ledger
from rain_footprint import build_sidecar


def example():
    lats = np.array([41.02, 41.01, 41.00])
    lons = np.array([-94.02, -94.01, -94.00])
    rates = np.zeros((3, 3)); rates[1, 1] = 1.5
    previous_sidecar = build_sidecar(lats, lons, rates, datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc), "synthetic-iowa")
    sidecar = build_sidecar(lats, lons, rates, datetime(2026, 7, 30, 0, 35, tzinfo=timezone.utc), "synthetic-iowa-next")
    previous = build_opportunity_ledger(previous_sidecar)
    return sidecar, previous, build_opportunity_ledger(sidecar, previous=previous)


def camera_catalog(ledger, bearing_offset=0):
    opportunity = ledger["opportunities"][0]
    first = opportunity["observerSwath"]["representativeCandidates"][0]
    when = datetime.fromisoformat(ledger["scanTime"].replace("Z", "+00:00"))
    _, sun_bearing = solar_position(when, first["lat"], first["lon"])
    return [{"id": 1, "name": "Test airport", "lat": first["lat"], "lon": first["lon"],
             "cameras": [{"id": 10, "bearing": (sun_bearing + 180 + bearing_offset) % 360,
                          "direction": "calibrated", "mapWedgeAngle": 45}]}]


class LedgerReviewTests(unittest.TestCase):
    def test_requires_persistence_and_camera_geometry_and_caps_at_two(self):
        sidecar, previous, ledger = example()
        catalog = camera_catalog(ledger)
        result = select_ledger_review_records(ledger, previous, sidecar, catalog)
        self.assertLessEqual(result["selected"], MAX_PER_SCAN)
        self.assertEqual(result["selected"], 1)
        record = result["records"][0]
        self.assertEqual(record["features"]["researchReview"]["source"], "opportunity_ledger")
        self.assertEqual(record["features"]["researchReview"]["currentDetectorDisposition"], "not_generated_as_detector_candidate")
        self.assertEqual(record["features"]["researchReview"]["persistenceScans"], 2)
        self.assertEqual(record["features"]["researchReview"]["scanGapMinutes"], 5)
        self.assertTrue(record["features"]["rain"]["antiSolarRainSpanByTier"])

    def test_wrong_facing_camera_cannot_satisfy_geometry_gate(self):
        sidecar, previous, ledger = example()
        self.assertEqual(select_ledger_review_records(ledger, previous, sidecar, camera_catalog(ledger, 100))["selected"], 0)

    def test_first_scan_never_enters_review(self):
        sidecar, _, ledger = example()
        self.assertEqual(select_ledger_review_records(ledger, None, sidecar, [])["selected"], 0)

    def test_stale_previous_scan_cannot_claim_persistence(self):
        sidecar, previous, ledger = example()
        previous["scanTime"] = "2026-07-29T23:30:00Z"
        self.assertEqual(select_ledger_review_records(ledger, previous, sidecar, camera_catalog(ledger))["selected"], 0)

    def test_packaged_faa_catalog_is_available(self):
        catalog = load_faa_catalog()
        self.assertGreater(len(catalog), 900)
        self.assertTrue(any(site.get("cameras") for site in catalog))


if __name__ == "__main__":
    unittest.main()
