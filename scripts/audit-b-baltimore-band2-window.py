#!/usr/bin/env python
"""Offline test of a real plus/minus-15-minute Band-2 maximum for Baltimore."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import requests
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "api"))

from detector_core import solar_position  # noqa: E402
from goes_sample import (  # noqa: E402
    AWS_BUCKETS, download_key, satellite_for, scan_start_from_key,
)
from sunlight_v2 import (  # noqa: E402
    BAND2_PRODUCT, Band2Sampler, cleanup_shadow_cache, list_band2_keys, parse_utc,
)

AUDIT_DIR = ROOT / "validation" / "audit-b"
REPLAY = AUDIT_DIR / "baltimore-mrms-replay-v1.json"
OUTPUT = AUDIT_DIR / "baltimore-band2-window-replay.json"
CACHE = ROOT / ".worker-cache" / "baltimore-band2-window"
TIMES = ("2026-07-28T23:30:00Z", "2026-07-28T23:40:00Z")


def canonical_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def frame_feature(lat: float, lon: float, key: str, stamp: str, target_at: str, session: requests.Session) -> dict:
    position = satellite_for(lon, "auto")
    bucket = AWS_BUCKETS[position]
    path = download_key(bucket, key, CACHE / "band2", session=session)
    dataset = xr.open_dataset(path, mask_and_scale=True)
    frame_time = parse_utc(stamp)
    elevation, bearing = solar_position(frame_time, lat, lon)
    target_time = parse_utc(target_at)
    metadata = {
        "product": BAND2_PRODUCT, "channel": 2, "s3Key": key, "observedAt": stamp,
        "timeOffsetMinutes": round((frame_time - target_time).total_seconds() / 60, 1),
        "satellitePosition": position,
    }
    sampler = Band2Sampler(target_at, CACHE / "band2", session=session)
    sampler.datasets[position] = (metadata, dataset)
    try:
        feature = sampler.sample_candidate({
            "lat": lat, "lon": lon, "sunElevationDeg": elevation,
            "sunBearingDeg": bearing, "antiSolarBearingDeg": (bearing + 180) % 360,
        })
    finally:
        sampler.close()
    return {
        "observedAt": stamp, "timeOffsetMinutes": metadata["timeOffsetMinutes"],
        "sunElevationDeg": round(elevation, 2), "maxGapFraction": feature["maxGapFraction"],
        "scenarioGapFractions": {
            name: value.get("gapFraction") for name, value in feature["scenarios"].items()
        },
    }


def point_window(name: str, lat: float, lon: float, target_at: str, keys: list[str], session: requests.Session) -> dict:
    target_time = parse_utc(target_at)
    choices = []
    for key in keys:
        stamp = scan_start_from_key(key)
        if not stamp:
            continue
        offset = (parse_utc(stamp) - target_time).total_seconds() / 60
        if abs(offset) <= 15:
            choices.append((parse_utc(stamp), key, stamp))
    frames = [frame_feature(lat, lon, key, stamp, target_at, session) for _, key, stamp in sorted(choices)]
    peak = max(frames, key=lambda item: item["maxGapFraction"] if item["maxGapFraction"] is not None else -1)
    closest = min(frames, key=lambda item: abs(item["timeOffsetMinutes"]))
    return {
        "name": name, "lat": lat, "lon": lon, "temporalFramesUsed": len(frames),
        "targetFrameGapFraction": closest["maxGapFraction"],
        "temporalMaxGapFraction": peak["maxGapFraction"], "peakObservedAt": peak["observedAt"],
        "frames": frames,
    }


def main() -> int:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    scans = {scan["observedAt"]: scan for scan in replay["scans"]}
    session = requests.Session()
    samples = []
    try:
        for target_at in TIMES:
            seed = scans[target_at]["strides"]["10"]["nearest"]["Baltimore"]
            position = satellite_for(replay["places"]["Baltimore"]["lon"], "auto")
            keys = list_band2_keys(AWS_BUCKETS[position], parse_utc(target_at), session)
            points = [
                point_window("Baltimore observer reference", **replay["places"]["Baltimore"], target_at=target_at, keys=keys, session=session),
                point_window("Dundalk observer reference", **replay["places"]["Dundalk"], target_at=target_at, keys=keys, session=session),
                point_window("nearest production-stride seed", lat=seed["observerLat"], lon=seed["observerLon"], target_at=target_at, keys=keys, session=session),
            ]
            samples.append({"targetAt": target_at, "points": points})
    finally:
        cleanup_shadow_cache(CACHE)

    payload = {
        "schemaVersion": 1, "methodVersion": "band2-temporal-window-offline-2026-07-v1",
        "purpose": "Test the requested plus/minus-15-minute Band-2 maximum on the Baltimore confirmed bow.",
        "observationWindow": replay["observationWindow"],
        "mrmsReplayContentSha256": hashlib.sha256(REPLAY.read_bytes()).hexdigest(),
        "interpretationGuard": "Offline sensitivity test only; it does not alter sunlight-v2 or public classification.",
        "samples": samples,
    }
    payload["contentSha256"] = canonical_hash(payload)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT), "contentSha256": payload["contentSha256"],
        "summary": [{
            "targetAt": sample["targetAt"],
            "points": [{
                "name": point["name"], "frames": point["temporalFramesUsed"],
                "targetGap": point["targetFrameGapFraction"], "temporalMaxGap": point["temporalMaxGapFraction"],
                "peakAt": point["peakObservedAt"],
            } for point in sample["points"]],
        } for sample in samples],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
