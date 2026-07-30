import gzip, json, math, unittest
from datetime import datetime, timezone
import numpy as np
from shapely.geometry import Point, shape
from detector_core import solar_position
from rain_footprint import build_sidecar
from map_tiers import MAX_GZIP_BYTES, build_map_tiers, encode_payload

class MapTierTests(unittest.TestCase):
    def fixture(self):
        lats=np.linspace(38.5,39.5,101);lons=np.linspace(-77.2,-76.2,101)
        rates=np.zeros((101,101));at=datetime(2026,7,28,23,34,tzinfo=timezone.utc)
        observer_lat,observer_lon=39.0,-76.7
        _,sun=solar_position(at,observer_lat,observer_lon);anti=(sun+180)%360
        for r,lat in enumerate(lats):
          for c,lon in enumerate(lons):
            north=(lat-observer_lat)*111;east=(lon-observer_lon)*111*math.cos(math.radians(observer_lat))
            distance=math.hypot(north,east);bearing=(math.degrees(math.atan2(east,north))+360)%360
            relative=(bearing-anti+180)%360-180
            if 8<=distance<=25 and abs(relative)<=25:rates[r,c]=2.0
        return build_sidecar(lats,lons,rates,at,"baltimore-synthetic")

    def test_partition_mask_and_size_budget(self):
        payload=build_map_tiers(self.fixture());rain=shape(payload["rain"]["geometry"]);green=shape(payload["aligned"]["geometry"])
        self.assertAlmostEqual(rain.intersection(green).area,0.0,places=10)
        self.assertLessEqual(len(encode_payload(payload)[1]),MAX_GZIP_BYTES)
        self.assertEqual(payload["tierRuleVersion"],"map-tiers-2026-07-v1")
        self.assertIn("generationLagSeconds",payload)
        for geometry in (rain,green):
            if not geometry.is_empty:
                self.assertTrue(geometry.centroid.within(shape(json.loads(__import__("pathlib").Path(__file__).parents[1].joinpath("docs/mockups/us-nation.geojson").read_text())["geometry"])))

    def test_baltimore_reference_has_green_corridor(self):
        text=__import__("pathlib").Path(__file__).parents[1].joinpath("docs/mockups/baltimore-scan-data.json").read_text(encoding="utf-8")
        reference=json.loads(text[text.index("{"):text.rindex("}")+1])
        corridor=shape(reference["aligned"]["geometry"])
        self.assertTrue(corridor.intersects(Point(-76.61,39.29).buffer(.15)))

if __name__=="__main__":unittest.main()
