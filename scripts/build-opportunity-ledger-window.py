#!/usr/bin/env python
"""Build private Opportunity Ledger proof records from archived MRMS GRIB files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from mrms_source import download_and_expand, key_for_time, object_from_key, open_precip_grid
from opportunity_ledger import build_opportunity_ledger
from rain_footprint import build_sidecar

STAMP = re.compile(r"_(\d{8})-(\d{6})\.grib2$")


def observed_at(path: Path) -> datetime:
    match = STAMP.search(path.name)
    if not match:
        raise ValueError("MRMS filename does not contain a timestamp: " + path.name)
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Archive times must include a timezone: " + value)
    return parsed.astimezone(timezone.utc)


def subset(field, bounds: list[float] | None):
    latitudes = field.coords["latitude"].values
    longitudes = field.coords["longitude"].values.copy()
    longitudes[longitudes > 180] -= 360
    if not bounds:
        return latitudes, longitudes, field.values
    west, south, east, north = bounds
    rows = [index for index, value in enumerate(latitudes) if south <= float(value) <= north]
    columns = [index for index, value in enumerate(longitudes) if west <= float(value) <= east]
    if not rows or not columns:
        raise ValueError("Requested bounds do not intersect the MRMS grid")
    row_slice = slice(min(rows), max(rows) + 1)
    column_slice = slice(min(columns), max(columns) + 1)
    return latitudes[row_slice], longitudes[column_slice], field.values[row_slice, column_slice]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("grib", nargs="*")
    parser.add_argument("--times", nargs="+", help="UTC ISO scan times to fetch from NOAA's public MRMS archive")
    parser.add_argument("--cache-dir", default=str(ROOT / ".worker-cache" / "opportunity-ledger-mrms"))
    parser.add_argument("--bounds", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if not args.grib and not args.times:
        parser.error("provide local GRIB files or --times")
    grib_paths = [Path(value) for value in args.grib]
    for value in args.times or []:
        at = parse_time(value)
        obj = object_from_key(key_for_time(at))
        grib_paths.append(download_and_expand(obj, Path(args.cache_dir)))
    output = Path(args.output_dir); output.mkdir(parents=True, exist_ok=True)
    previous = None
    summaries = []
    for path in sorted(set(grib_paths)):
        field = open_precip_grid(path)
        try:
            latitudes, longitudes, rates = subset(field, args.bounds)
            at = observed_at(path)
            sidecar = build_sidecar(latitudes, longitudes, rates, at, "local-archive/" + path.name)
            ledger = build_opportunity_ledger(sidecar, previous=previous)
            ledger["proofBounds"] = args.bounds
            destination = output / ("opportunity-ledger-" + at.strftime("%Y%m%dT%H%M%SZ") + ".json")
            destination.write_text(json.dumps(ledger, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
            summaries.append({"path": str(destination), **ledger["stats"], "lineageEdges": len(ledger["lineageEdges"])})
            previous = ledger
        finally:
            field.close()
    print(json.dumps({"ledgers": summaries}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
