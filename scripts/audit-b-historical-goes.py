#!/usr/bin/env python
"""Freeze historical GOES ACMC/DSRF point samples for Audit B."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from goes_sample import (  # noqa: E402
    AWS_BUCKETS, DEFAULT_PRODUCT, FALLBACK_PRODUCT, decode_value, download_key,
    latlon_to_scan_xy, list_s3_prefix, nearest_index, projection_attrs,
    satellite_for, scan_start_from_key, variable_name,
)

DEFAULT_FIXTURE = ROOT / "validation" / "audit-b" / "regression-fixture-v1.json"
DEFAULT_OUTPUT = ROOT / "validation" / "audit-b" / "historical-goes-v1.json"
DEFAULT_CACHE = ROOT / ".worker-cache" / "audit-b-goes"


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def reference_time(event: dict) -> datetime:
    observation = event["observation"]
    value = observation.get("confirmedFrameAt") or observation.get("representativeAt")
    if value:
        return parse_utc(value)
    start, end = parse_utc(observation["windowStart"]), parse_utc(observation["windowEnd"])
    return start + (end - start) / 2


def candidate_keys(bucket: str, product: str, target: datetime) -> list[tuple[float, str, datetime]]:
    found = set()
    for hour_delta in (-1, 0, 1):
        when = target + timedelta(hours=hour_delta)
        prefix = f"{product}/{when.year}/{when.timetuple().tm_yday:03d}/{when.hour:02d}/"
        for key in list_s3_prefix(bucket, prefix):
            if key.endswith(".nc"):
                found.add(key)
    candidates = []
    for key in found:
        observed = scan_start_from_key(key)
        if observed:
            scan = parse_utc(observed)
            candidates.append((abs((scan - target).total_seconds()), key, scan))
    return sorted(candidates)


def historical_bucket(position: str, target: datetime) -> tuple[str, str]:
    """Resolve the operational GOES satellite for the historical timestamp."""
    if position == "east" and target < datetime(2025, 4, 7, tzinfo=timezone.utc):
        return "noaa-goes16", "GOES-16"
    if position == "west" and target < datetime(2023, 1, 10, tzinfo=timezone.utc):
        return "noaa-goes17", "GOES-17"
    return AWS_BUCKETS[position], "GOES-19" if position == "east" else "GOES-18"


def sample_historical(lat: float, lon: float, target: datetime, product: str, cache_dir: Path) -> dict:
    satellite = satellite_for(lon, "auto")
    bucket, satellite_name = historical_bucket(satellite, target)
    attempts = []
    for offset_seconds, key, scan in candidate_keys(bucket, product, target):
        if offset_seconds > 30 * 60:
            break
        try:
            path = download_key(bucket, key, cache_dir)
            with xr.open_dataset(path, mask_and_scale=True) as dataset:
                var_name = variable_name(dataset, product)
                x, y = latlon_to_scan_xy(lat, lon, projection_attrs(dataset))
                xs, ys = dataset["x"].values, dataset["y"].values
                if not min(xs) <= x <= max(xs) or not min(ys) <= y <= max(ys):
                    raise RuntimeError("point outside product grid")
                ix, iy = nearest_index(xs, x), nearest_index(ys, y)
                value = decode_value(dataset[var_name].isel(x=ix, y=iy).values)
                return {
                    "available": True, "product": product, "variable": var_name,
                    "satellite": satellite_name,
                    "observedAt": scan.isoformat().replace("+00:00", "Z"),
                    "sampleOffsetMinutes": round((scan - target).total_seconds() / 60, 2),
                    "value": value, "units": dataset[var_name].attrs.get("units"), "s3Key": key,
                }
        except Exception as exc:
            attempts.append(f"{key}: {exc}")
    return {"available": False, "product": product, "reason": attempts[-1] if attempts else "no scan within 30 minutes"}


def canonical_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    selected = [event for event in fixture["events"] if event["productionScope"] == "conus_recall" and event["evidenceClass"] == "camera_confirmed" and isinstance(event["observer"].get("lat"), (int, float))]
    rows = []
    for index, event in enumerate(selected, 1):
        target, observer = reference_time(event), event["observer"]
        print(f"[{index}/{len(selected)}] {event['id']} {target.isoformat()}", flush=True)
        rows.append({
            "eventId": event["id"], "targetAt": target.isoformat().replace("+00:00", "Z"),
            "goesDsrf": sample_historical(observer["lat"], observer["lon"], target, DEFAULT_PRODUCT, args.cache_dir),
            "goesAcmc": sample_historical(observer["lat"], observer["lon"], target, FALLBACK_PRODUCT, args.cache_dir),
        })
    payload = {"schemaVersion": 1, "fixtureId": fixture["fixtureId"], "fixtureContentSha256": fixture["contentSha256"], "events": rows}
    payload["contentSha256"] = canonical_hash(payload)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "events": len(rows), "sha256": payload["contentSha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
