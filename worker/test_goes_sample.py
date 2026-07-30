import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import xarray as xr

API_DIR = Path(__file__).resolve().parents[1] / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from goes_sample import _TARGET_KEY_CACHE, dsrf_quality_metadata, key_near_time, object_creation_from_key


class GoesSampleQualityTests(unittest.TestCase):
    def test_timestamp_target_selects_nearest_frame_deterministically(self):
        _TARGET_KEY_CACHE.clear()
        keys = [
            "ABI-L2-ACMC/2026/210/21/OR_ABI-L2-ACMC-M6_G19_s20262102120170_e.nc",
            "ABI-L2-ACMC/2026/210/21/OR_ABI-L2-ACMC-M6_G19_s20262102130170_e.nc",
        ]
        target = datetime(2026, 7, 29, 21, 27, tzinfo=timezone.utc)
        with patch("goes_sample.list_s3_prefix", return_value=keys):
            selected = key_near_time("bucket", "ABI-L2-ACMC", target)
        self.assertIn("213017", selected)

    def test_dsrf_quality_metadata_uses_pixel_dqf_and_declared_bounds(self):
        dataset = xr.Dataset(
            data_vars={
                "DQF": (("y", "x"), np.array([[0, 1]], dtype=np.uint8)),
                "quantitative_solar_zenith_angle_bounds": (("number_of_bounds",), np.array([0.0, 70.0])),
                "retrieval_solar_zenith_angle_bounds": (("number_of_bounds",), np.array([0.0, 90.0])),
            },
            coords={"x": [0, 1], "y": [0], "number_of_bounds": [0, 1]},
        )
        quality = dsrf_quality_metadata(dataset, 1, 0)
        self.assertEqual(quality["dqf"], 1)
        self.assertEqual(quality["dqfMeaning"], "degraded_or_invalid")
        self.assertEqual(quality["quantitativeSolarZenithBoundsDeg"], [0.0, 70.0])
        self.assertEqual(quality["retrievalSolarZenithBoundsDeg"], [0.0, 90.0])

    def test_causal_selection_excludes_frame_created_after_assessment(self):
        _TARGET_KEY_CACHE.clear()
        keys = [
            "ABI-L2-ACMC/2026/210/23/OR_ABI-L2-ACMC-M6_G19_s20262102330170_e20262102333550_c20262102346020.nc",
            "ABI-L2-ACMC/2026/210/23/OR_ABI-L2-ACMC-M6_G19_s20262102325170_e20262102328550_c20262102332020.nc",
        ]
        target = datetime(2026, 7, 29, 23, 30, tzinfo=timezone.utc)
        cutoff = datetime(2026, 7, 29, 23, 36, tzinfo=timezone.utc)
        with patch("goes_sample.list_s3_prefix", return_value=keys):
            selected = key_near_time("bucket", "ABI-L2-ACMC", target, available_by=cutoff)
        self.assertIn("232517", selected)
        self.assertEqual(object_creation_from_key(keys[0]), "2026-07-29T23:46:02Z")


if __name__ == "__main__":
    unittest.main()
