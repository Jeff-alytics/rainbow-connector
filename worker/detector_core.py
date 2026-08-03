"""Pure radar-first geometry used by the NOAA ingestion worker."""

from __future__ import annotations

import math
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np

RAD = math.pi / 180
RAIN_MIN_MM_HR = 0.05
RAIN_MAX_MM_HR = 20.0
OBSERVER_DRY_MAX_MM_HR = 0.05
OBSERVER_DISTANCES_KM = (8, 15, 25, 40)
CONUS_BOUNDS = (-125.0, 24.0, -66.0, 50.0)
SPATIAL_SUPPORT_METHOD_VERSION = "mrms-adjacent-wet-cell-v1"


@lru_cache(maxsize=1)
def _conus_polygons() -> tuple[tuple[tuple[float, float, float, float], list], ...]:
    payload = json.loads(Path(__file__).with_name("conus-land.json").read_text(encoding="utf-8"))
    geometry = payload["geometry"]
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    prepared = []
    for polygon in polygons:
        points = [point for ring in polygon for point in ring]
        bounds = (
            min(point[0] for point in points), min(point[1] for point in points),
            max(point[0] for point in points), max(point[1] for point in points),
        )
        prepared.append((bounds, polygon))
    return tuple(prepared)


def _inside_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    previous = ring[-1]
    for current in ring:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def is_conus_land(lat: float, lon: float) -> bool:
    """True only for physical land inside the contiguous United States."""
    west, south, east, north = CONUS_BOUNDS
    if not south <= lat <= north or not west <= lon <= east:
        return False
    for (min_lon, min_lat, max_lon, max_lat), polygon in _conus_polygons():
        if not min_lon <= lon <= max_lon or not min_lat <= lat <= max_lat:
            continue
        if _inside_ring(lon, lat, polygon[0]) and not any(
            _inside_ring(lon, lat, hole) for hole in polygon[1:]
        ):
            return True
    return False


def solar_position(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    """Return approximate solar elevation and bearing in degrees."""
    when = when.astimezone(timezone.utc)
    day = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    equation = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    solar_minutes = hour * 60 + equation + 4 * lon
    hour_angle = (solar_minutes / 4 - 180) * RAD
    phi = lat * RAD
    cosine_zenith = (
        math.sin(phi) * math.sin(decl)
        + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    )
    zenith = math.acos(max(-1, min(1, cosine_zenith)))
    elevation = 90 - zenith / RAD
    azimuth = math.atan2(
        math.sin(hour_angle),
        math.cos(hour_angle) * math.sin(phi) - math.tan(decl) * math.cos(phi),
    )
    bearing = (azimuth / RAD + 180) % 360
    return elevation, bearing


def offset(lat: float, lon: float, distance_km: float, bearing: float) -> tuple[float, float]:
    angle = bearing * RAD
    return (
        lat + distance_km / 111 * math.cos(angle),
        lon + distance_km / (111 * max(0.2, math.cos(lat * RAD))) * math.sin(angle),
    )


def nearest_index(values: np.ndarray, target: float) -> int:
    return int(np.abs(values - target).argmin())


def sample_grid(latitudes: np.ndarray, longitudes: np.ndarray, rates: np.ndarray, lat: float, lon: float) -> float:
    row = nearest_index(latitudes, lat)
    column = nearest_index(longitudes, lon)
    value = rates[row, column]
    return max(0.0, float(value)) if np.isfinite(value) else 0.0


def is_edge(rates: np.ndarray, row: int, column: int, radius: int) -> bool:
    r0, r1 = max(0, row - radius), min(rates.shape[0], row + radius + 1)
    c0, c1 = max(0, column - radius), min(rates.shape[1], column + radius + 1)
    neighborhood = rates[r0:r1, c0:c1]
    return bool(np.any(~np.isfinite(neighborhood)) or np.any(neighborhood < RAIN_MIN_MM_HR))


def rain_spatial_support(rates: np.ndarray, row: int, column: int) -> dict:
    """Describe whether a wet MRMS cell has any immediate spatial support.

    The initial shadow gate is intentionally narrow: only a wet center cell with
    zero wet cells in its eight-neighbor ring is flagged as an isolated speckle.
    """
    r0, r1 = max(0, row - 1), min(rates.shape[0], row + 2)
    c0, c1 = max(0, column - 1), min(rates.shape[1], column + 2)
    neighborhood = rates[r0:r1, c0:c1]
    wet = np.isfinite(neighborhood) & (neighborhood >= RAIN_MIN_MM_HR)
    adjacent_wet_cells = int(wet.sum()) - int(
        np.isfinite(rates[row, column]) and rates[row, column] >= RAIN_MIN_MM_HR
    )
    isolated = adjacent_wet_cells == 0
    return {
        "methodVersion": SPATIAL_SUPPORT_METHOD_VERSION,
        "adjacentWetCells": adjacent_wet_cells,
        "isolatedPixel": isolated,
        "passes": not isolated,
    }


def cluster(seeds: list[dict], radius_km: float = 20, maximum: int = 400) -> list[dict]:
    selected: list[dict] = []
    for seed in sorted(seeds, key=lambda item: item["radarScore"], reverse=True):
        if any(
            math.hypot(
                (seed["lat"] - old["lat"]) * 111,
                (seed["lon"] - old["lon"]) * 111 * math.cos(seed["lat"] * RAD),
            ) < radius_km
            for old in selected
        ):
            continue
        selected.append(seed)
        if len(selected) >= maximum:
            break
    return selected


def observer_seeds_from_rain_grid(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    rates: np.ndarray,
    observed_at: datetime,
    stride: int = 10,
    maximum: int = 400,
    diagnostics: dict | None = None,
    enforce_spatial_support: bool = False,
) -> list[dict]:
    """Find rain edges first, then place potential observers toward the Sun."""
    latitudes = np.asarray(latitudes, dtype=float)
    longitudes = np.asarray(longitudes, dtype=float)
    rates = np.asarray(rates, dtype=float)
    if rates.shape != (latitudes.size, longitudes.size):
        raise ValueError("MRMS rate grid must match its one-dimensional latitude/longitude coordinates")

    candidates: list[dict] = []
    counts = {
        "sampledWetCells": 0,
        "rainEdgeCells": 0,
        "spatialSupportAssessedRainEdges": 0,
        "spatialSupportFlaggedRainEdges": 0,
        "spatialSupportRejectedRainEdges": 0,
        "lowSunRainEdges": 0,
        "dryObserverSeeds": 0,
        "nonConusObserverSeeds": 0,
    }
    edge_radius = max(1, stride)
    for row in range(0, rates.shape[0], stride):
        for column in range(0, rates.shape[1], stride):
            rain_rate = rates[row, column]
            if not np.isfinite(rain_rate) or not RAIN_MIN_MM_HR <= rain_rate <= RAIN_MAX_MM_HR:
                continue
            counts["sampledWetCells"] += 1
            if not is_edge(rates, row, column, edge_radius):
                continue
            counts["rainEdgeCells"] += 1
            spatial_support = rain_spatial_support(rates, row, column)
            counts["spatialSupportAssessedRainEdges"] += 1
            if spatial_support["isolatedPixel"]:
                counts["spatialSupportFlaggedRainEdges"] += 1
                if enforce_spatial_support:
                    counts["spatialSupportRejectedRainEdges"] += 1
                    continue
            rain_lat = float(latitudes[row])
            rain_lon = float(longitudes[column])
            elevation, sun_bearing = solar_position(observed_at, rain_lat, rain_lon)
            if not 0 <= elevation <= 30:
                continue
            counts["lowSunRainEdges"] += 1
            for distance in OBSERVER_DISTANCES_KM:
                observer_lat, observer_lon = offset(rain_lat, rain_lon, distance, sun_bearing)
                if not is_conus_land(observer_lat, observer_lon):
                    counts["nonConusObserverSeeds"] += 1
                    continue
                observer_rain = sample_grid(latitudes, longitudes, rates, observer_lat, observer_lon)
                if observer_rain >= OBSERVER_DRY_MAX_MM_HR:
                    continue
                sun_score = math.exp(-((elevation - 13) / 8) ** 2)
                rain_score = min(1, math.log1p(rain_rate) / math.log(5))
                edge_score = max(0, 1 - observer_rain / OBSERVER_DRY_MAX_MM_HR)
                counts["dryObserverSeeds"] += 1
                candidates.append({
                    "lat": round(observer_lat, 4),
                    "lon": round(observer_lon, 4),
                    "rainLat": round(rain_lat, 4),
                    "rainLon": round(rain_lon, 4),
                    "rainDistanceKm": distance,
                    "rainRateMmHr": round(float(rain_rate), 3),
                    "observerRainRateMmHr": round(observer_rain, 3),
                    "sunElevationDeg": round(elevation, 2),
                    "sunBearingDeg": round(sun_bearing, 1),
                    "antiSolarBearingDeg": round((sun_bearing + 180) % 360, 1),
                    "radarScore": round(100 * (0.45 * sun_score + 0.35 * rain_score + 0.2 * edge_score), 1),
                    "spatialSupport": spatial_support,
                })
                break
    selected = cluster(candidates, maximum=maximum)
    if diagnostics is not None:
        diagnostics.update({
            **counts,
            "spatialSupportMethodVersion": SPATIAL_SUPPORT_METHOD_VERSION,
            "spatialSupportMode": "enforce" if enforce_spatial_support else "shadow",
            "clusteredObserverSeeds": len(selected),
            "clusteredFlaggedObserverSeeds": sum(
                seed.get("spatialSupport", {}).get("isolatedPixel") is True for seed in selected
            ),
        })
    return selected
