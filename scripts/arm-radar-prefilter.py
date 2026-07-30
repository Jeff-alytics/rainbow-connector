#!/usr/bin/env python3
"""Rank ARM all-sky candidate times using archived NEXRAD reflectivity.

The IEM composite is used only as a retrospective sampling aid. It is not part
of the production Rainbow Connector forecast. Radar is sampled in a fan in the
anti-solar direction, where illuminated droplets capable of producing a
rainbow must be located relative to the observer.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "validation" / "arm-lamont" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "validation" / "arm-lamont" / "radar-ranked.json"
IEM_NETCDF = (
    "https://mesonet.agron.iastate.edu/cgi-bin/request/"
    "raster2netcdf.py?dstr={stamp}&prod=composite_n0q"
)
DISTANCES_KM = (5, 10, 15, 22, 30, 40, 48)
BEARING_OFFSETS_DEG = (-28, -16, -8, 0, 8, 16, 28)


def destination(lat: float, lon: float, bearing: float, distance_km: float):
    radius_km = 6371.0088
    angular = distance_km / radius_km
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing)
    phi2 = math.asin(
        math.sin(phi1) * math.cos(angular)
        + math.cos(phi1) * math.sin(angular) * math.cos(theta)
    )
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(angular) * math.cos(phi1),
        math.cos(angular) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), math.degrees(lam2)


def nearest_index(value: float, start: float, spacing: float, count: int) -> int:
    return max(0, min(count - 1, round((value - start) / spacing)))


def sample_radar(path: Path, lat: float, lon: float, bearing: float):
    with netCDF4.Dataset(path) as dataset:
        lats = dataset.variables["lat"]
        lons = dataset.variables["lon"]
        radar = dataset.variables["composite_n0q"]
        lat0, lat_step = float(lats[0]), float(lats[1] - lats[0])
        lon0, lon_step = float(lons[0]), float(lons[1] - lons[0])

        def at(sample_lat: float, sample_lon: float):
            yi = nearest_index(sample_lat, lat0, lat_step, len(lats))
            xi = nearest_index(sample_lon, lon0, lon_step, len(lons))
            value = radar[yi, xi]
            if getattr(value, "mask", False):
                return None
            number = float(value)
            return number if math.isfinite(number) and number < 1e10 else None

        observer = at(lat, lon)
        fan = []
        for distance in DISTANCES_KM:
            for offset in BEARING_OFFSETS_DEG:
                point_lat, point_lon = destination(lat, lon, bearing + offset, distance)
                fan.append(
                    {
                        "distanceKm": distance,
                        "bearingOffsetDeg": offset,
                        "dbz": at(point_lat, point_lon),
                    }
                )

    valid = [point["dbz"] for point in fan if point["dbz"] is not None]
    wet = [value for value in valid if value >= 5]
    moderate = [value for value in valid if value >= 20]
    by_distance = {}
    for distance in DISTANCES_KM:
        values = [
            point["dbz"]
            for point in fan
            if point["distanceKm"] == distance and point["dbz"] is not None
        ]
        by_distance[str(distance)] = {
            "maxDbz": max(values) if values else None,
            "wetPoints": sum(value >= 5 for value in values),
            "moderatePoints": sum(value >= 20 for value in values),
            "validPoints": len(values),
        }
    return {
        "observerDbz": observer,
        "fanMaxDbz": max(valid) if valid else None,
        "fanWetPoints": len(wet),
        "fanModeratePoints": len(moderate),
        "fanValidPoints": len(valid),
        "fanPointCount": len(fan),
        "byDistanceKm": by_distance,
    }


def download_and_sample(observed_at: str, site: dict, bearing: float):
    timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    timestamp = timestamp.astimezone(timezone.utc).replace(
        minute=(timestamp.minute // 5) * 5, second=0, microsecond=0
    )
    stamp = timestamp.strftime("%Y%m%d%H%M")
    url = IEM_NETCDF.format(stamp=stamp)
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "rainbow-connector-validation/1.0"})
        with urllib.request.urlopen(request, timeout=90) as response:
            temp_path.write_bytes(response.read())
        result = sample_radar(temp_path, site["lat"], site["lon"], bearing)
        result.update({"radarAt": timestamp.isoformat().replace("+00:00", "Z"), "sourceUrl": url})
        return result
    finally:
        temp_path.unlink(missing_ok=True)


def rank_score(event: dict, radar: dict) -> float:
    near_bins = [radar["byDistanceKm"][str(distance)] for distance in DISTANCES_KM if distance <= 22]
    near_values = [item["maxDbz"] for item in near_bins if item["maxDbz"] is not None]
    near_maximum = max(near_values) if near_values else -32
    near_wet = sum(item["wetPoints"] for item in near_bins)
    near_moderate = sum(item["moderatePoints"] for item in near_bins)
    observer = radar["observerDbz"]
    observer_penalty = 0 if observer is None or observer < 5 else 20 + observer * 0.5
    broad_rain_penalty = max(0, near_wet - 20) * 2
    # A visible bow needs nearby anti-solar droplets and direct sunlight, while
    # widespread rain at the observer usually means the sun/lens is obscured.
    return round(
        near_maximum
        + near_wet
        + near_moderate * 1.5
        + min(event["directNormalIrradianceWm2"], 500) / 35
        - observer_penalty
        - broad_rain_penalty,
        3,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    site = manifest["site"]
    ranked = []
    pairs = manifest["pairs"][: args.limit]
    for index, pair in enumerate(pairs, 1):
        event = pair["event"]
        print(f"[{index}/{len(pairs)}] {pair['id']} {event['observedAt']}", flush=True)
        try:
            radar = download_and_sample(
                event["observedAt"], site, event["antiSolarBearingDeg"]
            )
            score = rank_score(event, radar)
            error = None
        except Exception as exc:  # Preserve other results when one archive frame is unavailable.
            radar = None
            score = None
            error = f"{type(exc).__name__}: {exc}"
        ranked.append(
            {
                "pairId": pair["id"],
                "observedAt": event["observedAt"],
                "antiSolarBearingDeg": event["antiSolarBearingDeg"],
                "directNormalIrradianceWm2": event["directNormalIrradianceWm2"],
                "openMeteoPrecipitationMm": event["precipitationMm"],
                "radarScore": score,
                "radar": radar,
                "error": error,
            }
        )

    ranked.sort(key=lambda item: item["radarScore"] if item["radarScore"] is not None else -1e9, reverse=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Retrospective candidate ranking; not a production forecast input.",
        "method": {
            "source": "IEM nationwide NEXRAD N0Q composite",
            "timeResolutionMinutes": 5,
            "fanDistancesKm": list(DISTANCES_KM),
            "fanBearingOffsetsDeg": list(BEARING_OFFSETS_DEG),
            "wetThresholdDbz": 5,
            "moderateThresholdDbz": 20,
        },
        "site": site,
        "candidates": ranked,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
