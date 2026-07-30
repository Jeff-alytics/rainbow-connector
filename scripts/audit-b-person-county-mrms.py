#!/usr/bin/env python
"""Replay Person County MRMS seeds at production and finer strides."""

from __future__ import annotations

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
OUTPUT = ROOT / "validation" / "audit-b" / "person-county-mrms-replay-v1.json"
PLACE = {"name": "Person County", "lat": 36.39, "lon": -78.98, "uncertaintyKm": 25}


def distance_km(seed: dict) -> float:
    lat_scale = math.cos(math.radians(PLACE["lat"]))
    return math.hypot((seed["lat"] - PLACE["lat"]) * 111, (seed["lon"] - PLACE["lon"]) * 111 * lat_scale)


def aligned_slice(values, low: float, high: float, alignment: int = 10) -> slice:
    indices = [index for index, value in enumerate(values) if low <= float(value) <= high]
    if not indices:
        raise RuntimeError(f"No grid coordinates inside {low}..{high}")
    start = max(0, (min(indices) // alignment) * alignment)
    end = min(len(values), ((max(indices) + alignment) // alignment) * alignment + 1)
    return slice(start, end)


def compact(seed: dict, rank: int | None = None) -> dict:
    return {
        "distanceKm": round(distance_km(seed), 1),
        "rank": rank,
        "observerLat": seed["lat"], "observerLon": seed["lon"],
        "rainLat": seed["rainLat"], "rainLon": seed["rainLon"],
        "rainDistanceKm": seed["rainDistanceKm"], "rainRateMmHr": seed["rainRateMmHr"],
        "sunElevationDeg": seed["sunElevationDeg"], "radarScore": seed["radarScore"],
        "observerRainRateMmHr": seed["observerRainRateMmHr"],
    }


def main() -> int:
    start = datetime(2026, 7, 28, 23, 20, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=10 * index) for index in range(11)]
    scans = []
    fields = {}
    for index, when in enumerate(times, 1):
        obj = object_from_key(key_for_time(when))
        print(f"[{index}/{len(times)}] {obj.observed_at.isoformat()}", flush=True)
        try:
            path = download_and_expand(obj, CACHE)
            field = open_precip_grid(path)
            latitudes = field.coords["latitude"].values
            longitudes = field.coords["longitude"].values.copy()
            longitudes[longitudes > 180] -= 360
            lat_slice = aligned_slice(latitudes, 35.0, 37.7)
            lon_slice = aligned_slice(longitudes, -80.7, -77.3)
            regional_lat = latitudes[lat_slice]
            regional_lon = longitudes[lon_slice]
            regional_rates = field.values[lat_slice, lon_slice]
            stride_rows = {}
            for stride in (10, 5, 2, 1):
                diagnostics = {}
                seeds = observer_seeds_from_rain_grid(
                    regional_lat, regional_lon, regional_rates, obj.observed_at,
                    stride=stride, maximum=2000, diagnostics=diagnostics,
                )
                nearest = min(seeds, key=distance_km) if seeds else None
                near_misses = sorted(
                    (seed for seed in seeds if 80 <= seed["radarScore"] < 90 or 15 < seed["rainDistanceKm"] <= 40),
                    key=lambda seed: (distance_km(seed), -seed["radarScore"]),
                )[:10]
                stride_rows[str(stride)] = {
                    "regionalSeedCount": len(seeds),
                    "nearest": compact(nearest) if nearest else None,
                    "fallbackNearMisses": [compact(seed) for seed in near_misses],
                    "diagnostics": diagnostics,
                }
            scans.append({"observedAt": obj.observed_at.isoformat().replace("+00:00", "Z"), "sourceKey": obj.key, "strides": stride_rows})
            fields[obj.observed_at] = (field, latitudes, longitudes)
            field.close()
        except Exception as exc:
            scans.append({"observedAt": obj.observed_at.isoformat().replace("+00:00", "Z"), "sourceKey": obj.key, "error": str(exc)})

    summary = {}
    for stride in (10, 5, 2, 1):
        candidates = [
            scan["strides"][str(stride)]["nearest"] for scan in scans
            if scan.get("strides") and scan["strides"][str(stride)]["nearest"]
        ]
        summary[f"stride{stride}Nearest"] = min(candidates, key=lambda row: row["distanceKm"]) if candidates else None

    payload = {
        "schemaVersion": 1,
        "purpose": "Diagnostic regional replay; classify Person County independently from Baltimore.",
        "place": PLACE,
        "fallbackNearMissDefinition": {
            "radarScoreBand": [80, 90],
            "rainDistanceBandKm": [15, 40],
            "note": "Logged for evaluation; not an automatic acceptance rule.",
        },
        "summary": summary,
        "scans": scans,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
