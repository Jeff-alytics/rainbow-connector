#!/usr/bin/env python
"""Measure the nationwide review-volume cost of broadening the radar fallback.

This replays raw MRMS scans because published candidate history does not retain
radar seeds rejected during sunlight enrichment. Counts are radar-only upper
bounds: some seeds would later obtain adequate DNI/GOES evidence and would not
need the fallback reason.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from detector_core import observer_seeds_from_rain_grid  # noqa: E402
from mrms_source import download_and_expand, key_for_time, object_from_key, open_precip_grid  # noqa: E402

CACHE = ROOT / ".worker-cache" / "audit-b-mrms"
DEFAULT_OUTPUT = ROOT / "validation" / "audit-b" / "fallback-threshold-sweep-v1.json"
THRESHOLDS = (88, 90, 92, 95)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def legacy(seed: dict) -> bool:
    return (
        seed["radarScore"] >= 90
        and 5 <= seed["sunElevationDeg"] <= 22
        and seed["rainDistanceKm"] <= 15
        and seed["observerRainRateMmHr"] <= 0.05
    )


def broadened(seed: dict, threshold: int) -> bool:
    return (
        seed["radarScore"] >= threshold
        and 5 <= seed["sunElevationDeg"] <= 22
        and seed["rainDistanceKm"] <= 40
        and seed["observerRainRateMmHr"] <= 0.05
    )


def dist_km(a: dict, b: dict) -> float:
    return math.hypot(
        (a["lat"] - b["lat"]) * 111,
        (a["lon"] - b["lon"]) * 111 * math.cos(math.radians(a["lat"])),
    )


def event_count(scans: list[dict], threshold: int) -> int:
    events: list[dict] = []
    for scan in scans:
        when = parse_time(scan["observedAt"])
        for seed in scan["newByThreshold"][str(threshold)]:
            match = next((
                event for event in reversed(events)
                if when - parse_time(event["lastSeenAt"]) <= timedelta(minutes=30)
                and dist_km(seed, event) <= 40
            ), None)
            if match:
                match["lastSeenAt"] = iso(when)
                match["scanCount"] += 1
            else:
                events.append({
                    "lat": seed["lat"], "lon": seed["lon"],
                    "firstSeenAt": iso(when), "lastSeenAt": iso(when), "scanCount": 1,
                })
    return len(events)


def compact(seed: dict) -> dict:
    return {
        "lat": seed["lat"], "lon": seed["lon"],
        "radarScore": seed["radarScore"],
        "rainDistanceKm": seed["rainDistanceKm"],
        "sunElevationDeg": seed["sunElevationDeg"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-07-28T12:00:00Z")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--step-minutes", type=int, default=10)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    start = parse_time(args.start)
    stop = start + timedelta(hours=args.hours)
    times = []
    cursor = start
    while cursor < stop:
        times.append(cursor)
        cursor += timedelta(minutes=args.step_minutes)

    scans = []
    for index, when in enumerate(times, 1):
        obj = object_from_key(key_for_time(when))
        print(f"[{index}/{len(times)}] {obj.observed_at.isoformat()}", flush=True)
        try:
            path = download_and_expand(obj, CACHE)
            field = open_precip_grid(path)
            latitudes = field.coords["latitude"].values
            longitudes = field.coords["longitude"].values.copy()
            longitudes[longitudes > 180] -= 360
            diagnostics = {}
            seeds = observer_seeds_from_rain_grid(
                latitudes, longitudes, field.values, obj.observed_at,
                stride=10, maximum=400, diagnostics=diagnostics,
            )
            field.close()
            legacy_seeds = [seed for seed in seeds if legacy(seed)]
            new_by_threshold = {
                str(threshold): [compact(seed) for seed in seeds if broadened(seed, threshold) and not legacy(seed)]
                for threshold in THRESHOLDS
            }
            scans.append({
                "observedAt": iso(obj.observed_at),
                "sourceKey": obj.key,
                "clusteredSeeds": len(seeds),
                "legacyFallbackRadarUpperBound": len(legacy_seeds),
                "newByThreshold": new_by_threshold,
                "topSixNewFallbackShareUpperBound": {
                    str(threshold): min(6, len(rows)) / 6 for threshold, rows in (
                        (threshold, new_by_threshold[str(threshold)]) for threshold in THRESHOLDS
                    )
                },
                "diagnostics": diagnostics,
            })
        except Exception as exc:
            scans.append({"observedAt": iso(obj.observed_at), "sourceKey": obj.key, "error": str(exc)})

    good = [scan for scan in scans if not scan.get("error")]
    duration_days = max(args.hours / 24, 1 / 24)
    summary = {}
    for threshold in THRESHOLDS:
        key = str(threshold)
        new_scan_detections = sum(len(scan["newByThreshold"][key]) for scan in good)
        crowded = sum(len(scan["newByThreshold"][key]) >= 6 for scan in good)
        summary[key] = {
            "newRadarSeedDetections": new_scan_detections,
            "newRadarSeedDetectionsPerDay": round(new_scan_detections / duration_days, 1),
            "newUniqueEvents": event_count(good, threshold),
            "newUniqueEventsPerDay": round(event_count(good, threshold) / duration_days, 1),
            "scansWithAtLeastOneNewSeed": sum(bool(scan["newByThreshold"][key]) for scan in good),
            "scansWithSixOrMoreNewSeeds": crowded,
            "fractionScansFallbackCouldFillTopSix": round(crowded / len(good), 3) if good else None,
        }
    payload = {
        "schemaVersion": 1,
        "purpose": "National base-rate measurement for a distance-broadened geometry fallback.",
        "window": {"start": iso(start), "stopExclusive": iso(stop), "stepMinutes": args.step_minutes},
        "ruleCompared": {
            "legacy": "radarScore >= 90; sun 5..22 deg; rain distance <= 15 km; observer dry",
            "candidateRules": "radarScore >= threshold; sun 5..22 deg; rain distance <= 40 km; observer dry",
            "thresholds": list(THRESHOLDS),
            "newDefinition": "candidate rule passes and legacy rule fails",
        },
        "interpretationGuard": "Radar-only upper bound, not predicted published-candidate volume. Sunlight enrichment can remove the fallback need or reject the candidate.",
        "completedScans": len(good),
        "failedScans": len(scans) - len(good),
        "summary": summary,
        "scans": scans,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
