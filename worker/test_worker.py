import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np

from detector_core import (
    is_conus_land, observer_seeds_from_rain_grid, rain_spatial_support, solar_position,
)
from mrms_source import candidate_keys, key_for_time, object_from_key, source_metadata


class MrmsSourceTests(unittest.TestCase):
    def test_key_round_trip(self):
        moment = datetime(2026, 7, 26, 18, 24, tzinfo=timezone.utc)
        key = key_for_time(moment)
        self.assertEqual(
            key,
            "CONUS/PrecipRate_00.00/20260726/MRMS_PrecipRate_00.00_20260726-182400.grib2.gz",
        )
        self.assertEqual(object_from_key(key).observed_at, moment)

    def test_candidate_keys_are_newest_first_and_even_minute(self):
        now = datetime(2026, 7, 26, 18, 29, 31, tzinfo=timezone.utc)
        keys = list(candidate_keys(now, lookback_minutes=4))
        self.assertIn("20260726-182600", keys[0])
        self.assertIn("20260726-182200", keys[-1])

    def test_source_metadata_enforces_six_minutes(self):
        obj = object_from_key(
            "CONUS/PrecipRate_00.00/20260726/MRMS_PrecipRate_00.00_20260726-182400.grib2.gz"
        )
        healthy = source_metadata(obj, datetime(2026, 7, 26, 18, 29, tzinfo=timezone.utc))
        stale = source_metadata(obj, datetime(2026, 7, 26, 18, 31, tzinfo=timezone.utc))
        self.assertTrue(healthy["fresh"])
        self.assertFalse(stale["fresh"])


class RadarFirstTests(unittest.TestCase):
    def test_v5_seed_mode_keeps_high_sun_wet_observers_without_cap_or_clustering(self):
        lats = np.linspace(34, 36, 201)
        lons = np.linspace(-87, -85, 201)
        rates = np.zeros((201, 201))
        rates[100, 100] = 1.0
        diagnostics = {}
        with patch("detector_core.solar_position", return_value=(35.0, 90.0)), \
             patch("detector_core.is_conus_land", return_value=True), \
             patch("detector_core.sample_grid", return_value=1.0):
            seeds = observer_seeds_from_rain_grid(
                lats, lons, rates, datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
                stride=10, maximum=None, diagnostics=diagnostics,
                sun_elevation_range=(-2, 42), require_dry_observer=False,
                retain_all_observer_distances=True, cluster_radius_km=0,
            )
        self.assertEqual(len(seeds), 4)
        self.assertTrue(all(seed["observerRainRateMmHr"] == 1 for seed in seeds))
        self.assertIsNone(diagnostics["candidateCap"])
        self.assertFalse(diagnostics["candidateCapHit"])
        self.assertEqual(diagnostics["clusterRadiusKm"], 0)

    def test_spatial_support_flags_only_isolated_wet_cells(self):
        rates = np.zeros((7, 7))
        rates[3, 3] = 1.0
        isolated = rain_spatial_support(rates, 3, 3)
        self.assertTrue(isolated["isolatedPixel"])
        self.assertEqual(isolated["adjacentWetCells"], 0)

        rates[3, 4] = 0.2
        supported = rain_spatial_support(rates, 3, 3)
        self.assertFalse(supported["isolatedPixel"])
        self.assertEqual(supported["adjacentWetCells"], 1)

    def test_spatial_support_is_shadowed_before_enforcement(self):
        lats = np.linspace(34, 36, 201)
        lons = np.linspace(-87, -85, 201)
        rates = np.zeros((201, 201))
        rates[100, 100] = 1.0
        observed = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        diagnostics = {}
        shadowed = observer_seeds_from_rain_grid(
            lats, lons, rates, observed, stride=10, diagnostics=diagnostics,
        )
        enforced = observer_seeds_from_rain_grid(
            lats, lons, rates, observed, stride=10, enforce_spatial_support=True,
        )
        self.assertTrue(shadowed)
        self.assertTrue(all(seed["spatialSupport"]["isolatedPixel"] for seed in shadowed))
        self.assertEqual(diagnostics["spatialSupportMode"], "shadow")
        self.assertGreater(diagnostics["spatialSupportFlaggedRainEdges"], 0)
        self.assertEqual(enforced, [])

    def test_conus_land_mask_excludes_foreign_and_water_observers(self):
        self.assertTrue(is_conus_land(41.88, -87.63))  # Chicago
        self.assertTrue(is_conus_land(29.95, -90.07))  # New Orleans
        self.assertFalse(is_conus_land(53.51, -112.11))  # Edmonton
        self.assertFalse(is_conus_land(21.31, -157.86))  # Honolulu
        self.assertFalse(is_conus_land(42.20, -87.20))  # Lake Michigan
        self.assertFalse(is_conus_land(29.00, -90.00))  # Gulf of Mexico

    def test_low_morning_sun_points_observers_sunward_of_rain_edge(self):
        lats = np.linspace(34, 36, 201)
        lons = np.linspace(-87, -85, 201)
        rates = np.zeros((201, 201))
        rates[90:111, 90:111] = 1.0
        observed = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
        elevation, bearing = solar_position(observed, 35, -86)
        self.assertGreater(elevation, 0)
        self.assertLess(elevation, 30)
        self.assertGreater(bearing, 45)
        self.assertLess(bearing, 135)
        seeds = observer_seeds_from_rain_grid(lats, lons, rates, observed, stride=10)
        self.assertTrue(seeds)
        self.assertTrue(any(seed["lon"] > seed["rainLon"] for seed in seeds))
        self.assertTrue(all(seed["observerRainRateMmHr"] < 0.05 for seed in seeds))


if __name__ == "__main__":
    unittest.main()
