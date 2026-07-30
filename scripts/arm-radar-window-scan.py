#!/usr/bin/env python3
"""Rank ARM camera candidates using small, time-resolved NEXRAD WMS crops."""

from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "validation" / "arm-lamont" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "validation" / "arm-lamont" / "radar-window-ranked.json"
WMS = "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad/n0q-t.cgi"
PALETTE_SOURCE = (
    "https://mesonet.agron.iastate.edu/archive/data/2025/06/14/"
    "GIS/uscomp/n0q_202506140000.png"
)
DISTANCES_KM = (5, 10, 15, 22, 30, 40, 48)
BEARING_OFFSETS_DEG = (-28, -16, -8, 0, 8, 16, 28)
BBOX = (-98.2, 35.9, -96.8, 37.3)
WIDTH = HEIGHT = 280


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "rainbow-connector-validation/1.0"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def palette_lookup() -> dict[tuple[int, int, int], float | None]:
    image = Image.open(BytesIO(fetch(PALETTE_SOURCE)))
    palette = image.getpalette()
    return {
        (palette[index * 3], palette[index * 3 + 1], palette[index * 3 + 2]): (
            None if index == 0 else -32.5 + index * 0.5
        )
        for index in range(256)
    }


def solar_elevation(date: datetime, lat: float, lon: float) -> float:
    rad = math.pi / 180
    days = date.timestamp() * 1000 / 86_400_000 - 0.5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + 0.98560028 * days)
    longitude = anomaly + rad * (
        1.9148 * math.sin(anomaly)
        + 0.02 * math.sin(2 * anomaly)
        + 0.0003 * math.sin(3 * anomaly)
    ) + rad * 102.9372 + math.pi
    declination = math.asin(math.sin(rad * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(
        math.sin(longitude) * math.cos(rad * 23.4397), math.cos(longitude)
    )
    latitude = lat * rad
    hour_angle = rad * (280.16 + 360.9856235 * days) + lon * rad - right_ascension
    return math.asin(
        math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)
    ) / rad


def destination(lat: float, lon: float, bearing: float, distance_km: float):
    angular = distance_km / 6371.0088
    phi1, lam1, theta = map(math.radians, (lat, lon, bearing))
    phi2 = math.asin(
        math.sin(phi1) * math.cos(angular)
        + math.cos(phi1) * math.sin(angular) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), math.degrees(lam2)


def pixel(lat: float, lon: float):
    min_lon, min_lat, max_lon, max_lat = BBOX
    x = round((lon - min_lon) / (max_lon - min_lon) * (WIDTH - 1))
    y = round((max_lat - lat) / (max_lat - min_lat) * (HEIGHT - 1))
    return max(0, min(WIDTH - 1, x)), max(0, min(HEIGHT - 1, y))


def wms_url(timestamp: datetime) -> str:
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": "nexrad-n0q-wmst",
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": ",".join(map(str, BBOX)),
        "WIDTH": str(WIDTH),
        "HEIGHT": str(HEIGHT),
        "FORMAT": "image/png",
        "TRANSPARENT": "TRUE",
        "TIME": timestamp.strftime("%Y-%m-%dT%H:%M:00Z"),
    }
    return f"{WMS}?{urllib.parse.urlencode(params)}"


def sample_snapshot(job: dict, palette: dict):
    timestamp = job["timestamp"]
    image = Image.open(BytesIO(fetch(wms_url(timestamp)))).convert("RGBA")

    def at(lat: float, lon: float):
        rgba = image.getpixel(pixel(lat, lon))
        if rgba[3] == 0:
            return None
        return palette.get(rgba[:3])

    observer = at(job["lat"], job["lon"])
    points = []
    for distance in DISTANCES_KM:
        for offset in BEARING_OFFSETS_DEG:
            lat, lon = destination(
                job["lat"], job["lon"], job["bearing"] + offset, distance
            )
            points.append(
                {"distanceKm": distance, "bearingOffsetDeg": offset, "dbz": at(lat, lon)}
            )

    by_distance = {}
    for distance in DISTANCES_KM:
        values = [
            point["dbz"]
            for point in points
            if point["distanceKm"] == distance and point["dbz"] is not None
        ]
        by_distance[str(distance)] = {
            "maxDbz": max(values) if values else None,
            "wetPoints": sum(value >= 5 for value in values),
            "moderatePoints": sum(value >= 20 for value in values),
        }

    near = [point for point in points if point["distanceKm"] <= 22]
    near_values = [point["dbz"] for point in near if point["dbz"] is not None]
    near_wet = [point for point in near if point["dbz"] is not None and point["dbz"] >= 5]
    near_moderate = [point for point in near if point["dbz"] is not None and point["dbz"] >= 20]
    near_max = max(near_values) if near_values else None
    observer_penalty = 0 if observer is None or observer < 5 else 25 + observer * 0.6
    broad_penalty = max(0, len(near_wet) - 16) * 2.5
    proximity_bonus = max(
        (
            max(0, 24 - point["distanceKm"]) * 0.55
            for point in near_wet
        ),
        default=0,
    )
    sun_elevation = solar_elevation(timestamp, job["lat"], job["lon"])
    score = (
        (near_max if near_max is not None else -32)
        + len(near_wet) * 1.4
        + len(near_moderate) * 1.2
        + proximity_bonus
        + min(job["dni"], 650) / 35
        - observer_penalty
        - broad_penalty
    )
    if not 0 < sun_elevation < 42:
        score = -1000
    return {
        "pairId": job["pairId"],
        "radarAt": timestamp.isoformat().replace("+00:00", "Z"),
        "offsetMinutes": job["offsetMinutes"],
        "score": round(score, 3),
        "observerDbz": observer,
        "nearMaxDbz": near_max,
        "nearWetPoints": len(near_wet),
        "nearModeratePoints": len(near_moderate),
        "byDistanceKm": by_distance,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    site = manifest["site"]
    pairs = manifest["pairs"][: args.limit]
    palette = palette_lookup()
    jobs = []
    for pair in pairs:
        event = pair["event"]
        center = datetime.fromisoformat(event["observedAt"].replace("Z", "+00:00"))
        center = center.astimezone(timezone.utc).replace(
            minute=(center.minute // 5) * 5, second=0, microsecond=0
        )
        for offset in range(-args.window_minutes, args.window_minutes + 1, 5):
            jobs.append(
                {
                    "pairId": pair["id"],
                    "timestamp": center + timedelta(minutes=offset),
                    "offsetMinutes": offset,
                    "lat": site["lat"],
                    "lon": site["lon"],
                    "bearing": event["antiSolarBearingDeg"],
                    "dni": event["directNormalIrradianceWm2"],
                }
            )

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 10))) as executor:
        futures = {executor.submit(sample_snapshot, job, palette): job for job in jobs}
        for complete, future in enumerate(as_completed(futures), 1):
            job = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "pairId": job["pairId"],
                        "radarAt": job["timestamp"].isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            if complete % 100 == 0 or complete == len(jobs):
                print(f"Scanned {complete}/{len(jobs)} radar frames", flush=True)

    candidates = []
    events = {pair["id"]: pair["event"] for pair in pairs}
    for pair in pairs:
        snapshots = [item for item in results if item["pairId"] == pair["id"]]
        snapshots.sort(key=lambda item: item["score"], reverse=True)
        candidates.append(
            {
                "pairId": pair["id"],
                "event": events[pair["id"]],
                "bestRadar": snapshots[0] if snapshots else None,
                "radarWindow": sorted(snapshots, key=lambda item: item["radarAt"]),
            }
        )
    candidates.sort(
        key=lambda item: item["bestRadar"]["score"] if item["bestRadar"] else -1e9,
        reverse=True,
    )
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Human-review candidate selection; not production forecast input.",
        "method": {
            "source": "IEM NEXRAD N0Q WMS-T",
            "windowMinutes": args.window_minutes,
            "intervalMinutes": 5,
            "bbox": BBOX,
            "nearRangeKm": 22,
        },
        "candidates": candidates,
        "errors": errors,
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}; {len(errors)} frame errors")


if __name__ == "__main__":
    main()
