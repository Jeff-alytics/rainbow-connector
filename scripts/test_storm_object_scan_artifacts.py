import unittest
from datetime import datetime, timezone

import numpy as np

from scripts.build_storm_object_scan_artifacts import attach_seeds
from worker.opportunity_ledger import storm_objects
from worker.rain_footprint import build_storm_segmentation_sidecar


class StormObjectScanArtifactTests(unittest.TestCase):
    def test_seed_attachment_uses_native_rain_target_cell(self):
        rates = np.zeros((3, 5))
        rates[1, 1:4] = [3.0, 0.6, 3.0]
        sidecar = build_storm_segmentation_sidecar(
            np.array([40.02, 40.01, 40.00]),
            np.array([-100.04, -100.03, -100.02, -100.01, -100.00]),
            rates,
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            "synthetic",
        )
        objects = storm_objects(sidecar)
        attached, counts, unattached = attach_seeds(sidecar, objects, [
            {"rainLat": 40.01, "rainLon": -100.03},
            {"rainLat": 40.02, "rainLon": -100.00},
        ])
        self.assertEqual(len(attached), 1)
        self.assertEqual(sum(counts.values()), 1)
        self.assertEqual(unattached, 1)


if __name__ == "__main__":
    unittest.main()
