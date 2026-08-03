#!/usr/bin/env python
"""Re-fetch and verify the canonical MRMS artifact/real-shower controls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from detector_core import nearest_index, rain_spatial_support
from mrms_source import download_and_expand, object_from_key, open_precip_grid

CASES = (
    {
        "name": "Aberdeen isolated artifact",
        "key": "CONUS/PrecipRate_00.00/20260707/MRMS_PrecipRate_00.00_20260707-001000.grib2.gz",
        "lat": 45.195, "lon": -98.495, "expectedIsolated": True,
    },
    {
        "name": "Plant City coherent shower",
        "key": "CONUS/PrecipRate_00.00/20260709/MRMS_PrecipRate_00.00_20260709-230000.grib2.gz",
        "lat": 28.195, "lon": -81.995, "expectedIsolated": False,
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("C:/tmp/rainbow-spatial-controls"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = []
    for case in CASES:
        field = open_precip_grid(download_and_expand(
            object_from_key(case["key"]), args.cache_dir,
        ))
        latitudes = field.coords["latitude"].values
        longitudes = ((field.coords["longitude"].values + 180) % 360) - 180
        row = nearest_index(latitudes, case["lat"])
        column = nearest_index(longitudes, case["lon"])
        support = rain_spatial_support(field.values, row, column)
        result = {
            **case,
            "decodedRateMmHr": round(float(field.values[row, column]), 3),
            "spatialSupport": support,
            "passed": support["isolatedPixel"] is case["expectedIsolated"],
        }
        results.append(result)
    payload = {
        "schemaVersion": "spatial-support-control-check.v1",
        "passed": all(item["passed"] for item in results),
        "results": results,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
