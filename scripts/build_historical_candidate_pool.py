#!/usr/bin/env python
"""Build the historical FAA candidate pool from archived MRMS radar.

For every 10-minute MRMS timestamp in the study window whose sun geometry is
eligible somewhere near an FAA site, this runs the PRODUCTION candidate
generator (`observer_seeds_from_rain_grid`) on the archived PrecipRate grid and
keeps only seeds with a bow-facing FAA camera within 40 km (production
`camera_matches`). Output is one JSONL file per UTC day, written incrementally
and safely resumable. It downloads radar only — never FAA images — and touches
no production store.

The pool is raw: reviewed-batch / frozen-event exclusions and V1/V2 sunlight
replay are applied by later stages, so this stage never needs re-running when
those rules change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "worker", ROOT / "api"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from detector_core import observer_seeds_from_rain_grid, solar_position
from mrms_source import USER_AGENT, download_and_expand, key_for_time, object_from_key, open_precip_grid
from research_review import camera_matches

SCHEMA_VERSION = "historical-candidate-pool.v2"
FRAME_CADENCE_MINUTES = 10
SEED_SUN_MIN_DEG = 0.0   # observer_seeds_from_rain_grid gates rain edges to 0..30
SEED_SUN_MAX_DEG = 30.0
CAMERA_DISTANCE_KM = 40.0


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_catalog(path: Path) -> tuple[list[dict], str]:
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    raw = json.loads(raw_bytes.decode("utf-8-sig"))
    catalog = []
    for site in raw:
        cameras = [
            {"id": camera.get("cameraId"), "bearing": camera.get("cameraBearing"),
             "mapWedgeAngle": camera.get("mapWedgeAngle"), "direction": camera.get("cameraDirection")}
            for camera in site.get("cameras", [])
        ]
        if cameras and site.get("latitude") is not None and site.get("longitude") is not None:
            catalog.append({"id": site.get("siteId"), "name": site.get("siteName"),
                            "identifier": site.get("siteIdentifier"),
                            "lat": float(site["latitude"]), "lon": float(site["longitude"]),
                            "cameras": cameras})
    return catalog, digest


def eligible_frame_times(start: datetime, end_exclusive: datetime, catalog: list[dict]) -> list[datetime]:
    """10-minute stamps where at least one FAA site sees sun in the seed band.

    The seed generator itself gates every rain edge on 0-30 degrees; this is
    only a download filter, so it errs slightly wide (+/-1 degree) rather than
    trying to be exact.
    """
    probes = catalog[:: max(1, len(catalog) // 150)]
    times = []
    cursor = start
    while cursor < end_exclusive:
        for site in probes:
            elevation, _ = solar_position(cursor, site["lat"], site["lon"])
            if SEED_SUN_MIN_DEG - 1 <= elevation <= SEED_SUN_MAX_DEG + 1:
                times.append(cursor)
                break
        cursor += timedelta(minutes=FRAME_CADENCE_MINUTES)
    return times


def conus_eligible_frame_times(start: datetime, end_exclusive: datetime) -> list[datetime]:
    """Camera-independent stamps where some fixed CONUS probe has low sun."""
    probes = [
        (lat, lon)
        for lat in range(24, 51, 2)
        for lon in range(-124, -65, 2)
    ]
    times = []
    cursor = start
    while cursor < end_exclusive:
        if any(
            SEED_SUN_MIN_DEG - 2 <= solar_position(cursor, lat, lon)[0] <= SEED_SUN_MAX_DEG + 2
            for lat, lon in probes
        ):
            times.append(cursor)
        cursor += timedelta(minutes=FRAME_CADENCE_MINUTES)
    return times


def completed_frames(day_path: Path) -> set[str]:
    if not day_path.exists():
        return set()
    stamps = set()
    for line in day_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("status") in {"ok", "missing_mrms_object"}:
                stamps.add(row["observedAt"])
        except (ValueError, KeyError):
            continue
    return stamps


def process_frame(
    when: datetime,
    catalog: list[dict],
    cache_dir: Path,
    session: requests.Session,
    retain_all_seeds: bool = False,
    maximum_seeds: int = 5000,
) -> dict:
    obj = object_from_key(key_for_time(when))
    head = session.head(obj.url, headers={"User-Agent": USER_AGENT}, timeout=15)
    if head.status_code in (403, 404):
        return {"observedAt": iso(when), "status": "missing_mrms_object", "s3Key": obj.key,
                "seedCount": 0, "matchedCount": 0, "matched": []}
    head.raise_for_status()
    path = download_and_expand(obj, cache_dir, session=session)
    try:
        field = open_precip_grid(path)
        latitudes = field.coords["latitude"].values
        longitudes = field.coords["longitude"].values.copy()
        longitudes[longitudes > 180] -= 360
        rates = field.values
        diagnostics: dict = {}
        seeds = observer_seeds_from_rain_grid(latitudes, longitudes, rates, when,
                                              stride=10, maximum=maximum_seeds, diagnostics=diagnostics)
    finally:
        for stale in cache_dir.glob(path.name + "*"):
            try:
                stale.unlink()
            except OSError:
                pass
    matched = []
    for seed in seeds:
        record = {"features": {"observer": {"lat": seed["lat"], "lon": seed["lon"]},
                               "geometry": {"sunElevationDeg": seed["sunElevationDeg"],
                                            "antiSolarBearingDeg": seed["antiSolarBearingDeg"]}}}
        hits = camera_matches(record, catalog, maximum_distance_km=CAMERA_DISTANCE_KM)
        if hits:
            matched.append({**seed, "cameraMatches": hits})
    row = {"observedAt": iso(when), "status": "ok", "s3Key": obj.key,
            "seedCount": len(seeds), "matchedCount": len(matched),
            "diagnostics": diagnostics, "matched": matched}
    if retain_all_seeds:
        row["allSeeds"] = seeds
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-07-03T00:00:00Z")
    parser.add_argument("--end", default="2026-07-31T00:00:00Z", help="exclusive")
    parser.add_argument("--sites", type=Path, default=ROOT / "validation/faa/sites.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation/historical-candidate-pool")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--limit-frames", type=int, default=0, help="stop after N frames (test batches)")
    parser.add_argument("--maximum-seeds", type=int, default=5000)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument(
        "--retain-all-seeds", action="store_true",
        help="retain every pre-camera seed and use camera-independent CONUS frame selection",
    )
    args = parser.parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be in [0, --shard-count)")

    start, end = parse_utc(args.start), parse_utc(args.end)
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = args.cache_dir or output_dir / "mrms-cache"

    catalog, catalog_sha = load_catalog(args.sites)
    frames = (
        conus_eligible_frame_times(start, end)
        if args.retain_all_seeds else eligible_frame_times(start, end, catalog)
    )
    frames = [
        frame for index, frame in enumerate(frames)
        if index % args.shard_count == args.shard_index
    ]
    print(f"window {iso(start)} .. {iso(end)}: {len(frames)} sun-eligible frames "
          f"({FRAME_CADENCE_MINUTES}-min cadence), {len(catalog)} FAA sites", flush=True)

    manifest_path = output_dir / "pool-manifest.json"
    manifest = {
        "schemaVersion": SCHEMA_VERSION, "window": {"start": iso(start), "endExclusive": iso(end)},
        "frameCadenceMinutes": FRAME_CADENCE_MINUTES, "cameraDistanceKm": CAMERA_DISTANCE_KM,
        "inputScope": "camera_independent_nationwide" if args.retain_all_seeds else "camera_filtered_legacy",
        "retainsAllSeeds": args.retain_all_seeds,
        "frameSelection": "fixed_conus_probe_grid" if args.retain_all_seeds else "faa_site_probe",
        "maximumSeedsPerFrame": args.maximum_seeds,
        "shard": {"index": args.shard_index, "count": args.shard_count},
        "seedGenerator": (
            "worker.detector_core.observer_seeds_from_rain_grid"
            f"(stride=10, maximum={args.maximum_seeds})"
        ),
        "cameraMatcher": "worker.research_review.camera_matches",
        "mrmsSource": "s3://noaa-mrms-pds CONUS PrecipRate_00.00 (archived)",
        "siteCatalog": str(args.sites), "siteCatalogSha256": catalog_sha,
        "note": "raw pool; exclusions and V1/V2 sunlight replay are applied downstream",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    session = requests.Session()
    processed = 0
    for when in frames:
        day_path = output_dir / f"pool-{when.strftime('%Y-%m-%d')}.jsonl"
        if iso(when) in completed_frames(day_path):
            continue
        started = time.time()
        try:
            row = process_frame(
                when, catalog, cache_dir, session,
                retain_all_seeds=args.retain_all_seeds,
                maximum_seeds=args.maximum_seeds,
            )
        except Exception as error:  # record and continue; a single bad frame must not kill a day
            row = {"observedAt": iso(when), "status": f"error: {str(error)[:200]}",
                   "seedCount": 0, "matchedCount": 0, "matched": []}
            if args.retain_all_seeds:
                row["allSeeds"] = []
        with day_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        processed += 1
        print(f"{row['observedAt']} {row['status']}: seeds={row['seedCount']} "
              f"cameraMatched={row['matchedCount']} ({time.time() - started:.1f}s)", flush=True)
        if args.limit_frames and processed >= args.limit_frames:
            print(f"stopping after --limit-frames={args.limit_frames}")
            break
    print(f"done: {processed} frames processed this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
