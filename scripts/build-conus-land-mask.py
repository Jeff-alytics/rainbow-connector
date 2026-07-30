"""Build the small runtime CONUS land mask from Natural Earth shapefiles.

Development-only dependencies: pyshp and shapely. The worker reads the emitted
GeoJSON with its own point-in-polygon code, so Lambda gains no dependencies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import shapefile
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union


def geometries(path: Path):
    reader = shapefile.Reader(str(path))
    return reader, [shape(item.__geo_interface__) for item in reader.shapes()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--countries", required=True, type=Path)
    parser.add_argument("--land", required=True, type=Path)
    parser.add_argument("--lakes", required=True, type=Path)
    parser.add_argument("--output", default="worker/conus-land.json", type=Path)
    args = parser.parse_args()

    country_reader = shapefile.Reader(str(args.countries))
    usa = next(
        shape(item.shape.__geo_interface__)
        for item in country_reader.iterShapeRecords()
        if item.record["ADM0_A3"] == "USA"
    )
    conus_box = box(-125.0, 24.0, -66.0, 50.0)
    _, land_geometries = geometries(args.land)
    _, lake_geometries = geometries(args.lakes)
    land = unary_union([item for item in land_geometries if item.intersects(conus_box)])
    lakes = unary_union([item for item in lake_geometries if item.intersects(conus_box)])
    conus_land = usa.intersection(conus_box).intersection(land).difference(lakes).simplify(0.005, preserve_topology=True)

    payload = {
        "source": "Natural Earth 1:50m land, lakes, and admin-0 boundaries",
        "bounds": [-125.0, 24.0, -66.0, 50.0],
        "geometry": mapping(conus_land),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
