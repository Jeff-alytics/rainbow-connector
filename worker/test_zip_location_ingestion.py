import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_zip_location_points import build_locations, load_tract_centers


class ZipLocationIngestionTests(unittest.TestCase):
    def test_committed_artifact_covers_every_geographic_zip(self):
        root = Path(__file__).resolve().parents[1]
        with (root / 'zip-centroids.json').open(encoding='utf-8') as handle:
            geographic = json.load(handle)
        with (root / 'data' / 'zip-location-points-v1.json').open(encoding='utf-8') as handle:
            artifact = json.load(handle)
        self.assertEqual(set(artifact['locations']), set(geographic))
        self.assertEqual(sum(artifact['counts'].values()), len(geographic))

    def test_residential_ratio_weights_tract_centers(self):
        locations, counts = build_locations(
            {"12345": ["Example", "NY", 40.0, -73.0]},
            [
                {"zip": "12345", "geoid": "36001000100", "res_ratio": 0.75},
                {"zip": "12345", "geoid": "36001000200", "res_ratio": 0.25},
            ],
            {"36001000100": (40.0, -73.0), "36001000200": (44.0, -69.0)},
        )
        self.assertEqual(locations["12345"]["locationMethod"], "zip-population-weighted-v1")
        self.assertEqual(locations["12345"]["latitude"], 41.0)
        self.assertEqual(locations["12345"]["longitude"], -72.0)
        self.assertEqual(counts, {"zip-population-weighted-v1": 1})

    def test_shared_coordinate_requires_pin_even_with_residential_data(self):
        locations, _ = build_locations(
            {
                "12345": ["A", "NY", 40.0, -73.0],
                "12346": ["B", "NY", 40.0, -73.0],
            },
            [{"zip": "12345", "geoid": "36001000100", "res_ratio": 1}],
            {"36001000100": (40.1, -73.1)},
        )
        self.assertEqual(locations["12345"]["locationMethod"], "zip-shared-coordinate")
        self.assertTrue(locations["12345"]["requiresPin"])
        self.assertIsNone(locations["12345"].get("locationUncertaintyKm"))

    def test_no_residential_ratio_is_po_box_branch(self):
        locations, _ = build_locations(
            {"12345": ["Example", "NY", 40.0, -73.0]},
            [{"zip": "12345", "geoid": "36001000100", "res_ratio": 0}],
            {"36001000100": (40.1, -73.1)},
        )
        self.assertEqual(locations["12345"]["locationMethod"], "zip-po-box-only")
        self.assertTrue(locations["12345"]["requiresPin"])

    def test_census_geoid_is_zero_padded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tract.csv"
            path.write_text(
                "STATEFP,COUNTYFP,TRACTCE,POPULATION,LATITUDE,LONGITUDE\n"
                "1,1,1,10,+40.0,-73.0\n",
                encoding="utf-8",
            )
            self.assertEqual(load_tract_centers([path]), {"01001000001": (40.0, -73.0)})


if __name__ == "__main__":
    unittest.main()
