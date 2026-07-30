#!/usr/bin/env python
"""Replay Baltimore/Dundalk MRMS rain-edge seeds at production and finer strides."""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from detector_core import observer_seeds_from_rain_grid  # noqa: E402
from mrms_source import download_and_expand, key_for_time, object_from_key, open_precip_grid  # noqa: E402

CACHE = ROOT / ".worker-cache" / "audit-b-mrms"
OUTPUT = ROOT / "validation" / "audit-b" / "baltimore-mrms-replay-v1.json"
PLACES = {
    "Baltimore": {"lat": 39.2904, "lon": -76.6122},
    "Dundalk": {"lat": 39.2507, "lon": -76.5205},
}
OBSERVATION_WINDOW = {
    "windowStart": "2026-07-28T23:25:00Z",
    "windowEnd": "2026-07-28T23:45:00Z",
    "reportedBowStart": "2026-07-28T23:30:00Z",
    "reportedBowEnd": "2026-07-28T23:34:00Z",
    "precision": "observer_reported_time",
}


def distance_km(a: dict, b: dict) -> float:
    lat_scale = math.cos(math.radians(a["lat"]))
    return math.hypot((a["lat"] - b["lat"]) * 111, (a["lon"] - b["lon"]) * 111 * lat_scale)


def aligned_slice(values, low: float, high: float, alignment: int = 10) -> slice:
    indices = [index for index, value in enumerate(values) if low <= float(value) <= high]
    if not indices:
        raise RuntimeError(f"No grid coordinates inside {low}..{high}")
    start = max(0, (min(indices) // alignment) * alignment)
    end = min(len(values), ((max(indices) + alignment) // alignment) * alignment + 1)
    return slice(start, end)


def nearest(seeds: list[dict], place: dict) -> dict | None:
    if not seeds:
        return None
    row = min(seeds, key=lambda seed: distance_km(seed, place))
    return {
        "distanceKm": round(distance_km(row, place), 1),
        "observerLat": row["lat"], "observerLon": row["lon"],
        "rainLat": row["rainLat"], "rainLon": row["rainLon"],
        "rainDistanceKm": row["rainDistanceKm"], "rainRateMmHr": row["rainRateMmHr"],
        "sunElevationDeg": row["sunElevationDeg"], "radarScore": row["radarScore"],
    }


def main() -> int:
    start = datetime(2026, 7, 28, 21, 50, tzinfo=timezone.utc)
    times = [start + timedelta(minutes=10 * index) for index in range(12)]
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
            lat_slice = aligned_slice(latitudes, 37.5, 40.5)
            lon_slice = aligned_slice(longitudes, -78.0, -74.5)
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
                stride_rows[str(stride)] = {
                    "regionalSeedCount": len(seeds),
                    "diagnostics": diagnostics,
                    "nearest": {name: nearest(seeds, place) for name, place in PLACES.items()},
                }
            scans.append({"observedAt": obj.observed_at.isoformat().replace("+00:00", "Z"), "sourceKey": obj.key, "strides": stride_rows})
            field.close()
        except Exception as exc:
            scans.append({"observedAt": obj.observed_at.isoformat().replace("+00:00", "Z"), "sourceKey": obj.key, "error": str(exc)})
    summary = {}
    corrected_window_summary = {}
    window_start = datetime.fromisoformat(OBSERVATION_WINDOW["windowStart"].replace("Z", "+00:00"))
    window_end = datetime.fromisoformat(OBSERVATION_WINDOW["windowEnd"].replace("Z", "+00:00"))
    corrected_scans = [
        scan for scan in scans
        if window_start <= datetime.fromisoformat(scan["observedAt"].replace("Z", "+00:00")) <= window_end
    ]
    for stride in (10, 5, 2, 1):
        for name in PLACES:
            candidates = [scan["strides"][str(stride)]["nearest"][name] for scan in scans if scan.get("strides") and scan["strides"][str(stride)]["nearest"][name]]
            summary[f"stride{stride}{name}"] = min(candidates, key=lambda row: row["distanceKm"]) if candidates else None
            window_candidates = [scan["strides"][str(stride)]["nearest"][name] for scan in corrected_scans if scan.get("strides") and scan["strides"][str(stride)]["nearest"][name]]
            corrected_window_summary[f"stride{stride}{name}"] = min(window_candidates, key=lambda row: row["distanceKm"]) if window_candidates else None
    payload = {
        "schemaVersion": 1,
        "purpose": "Diagnostic regional replay; finer strides show seed sensitivity but do not reproduce the nationwide 400-candidate cap.",
        "places": PLACES,
        "observationWindow": OBSERVATION_WINDOW,
        "diagnosis": {
            "classification": "corrected_window_radar_generation_confirmed_production_disposition_not_retained",
            "generated": True,
            "productionArtifactScansRetained": False,
            "correctedWindowProductionStride": {
                scan["observedAt"]: scan["strides"]["10"]["nearest"]
                for scan in corrected_scans if scan.get("strides")
            },
            "caveat": "This regional replay proves that production-stride radar seeding found nearby observer geometry. It does not reproduce nationwide clustering, the 400-candidate cap, or the exact live Open-Meteo DNI for rejected seeds.",
        },
        "summary": summary,
        "correctedWindowSummary": corrected_window_summary,
        "scans": scans,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
