"""Deterministic MRMS rain-footprint sidecars and candidate rain-span features."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

SCHEMA_VERSION = "mrms-rain-footprint.v2"
RATE_TIERS_MM_HR = (0.05, 1.0, 5.0)
STORM_ENVELOPE_THRESHOLD_MM_HR = 0.5
STORM_CORE_THRESHOLD_MM_HR = 2.0
STORM_SEGMENTATION_VERSION = "mrms-hysteresis-0.5-envelope-2.0-core-v1"
DISPLAY_RAIN_THRESHOLD_MM_HR = 0.2
MIN_DISTANCE_KM = 5.0
MAX_DISTANCE_KM = 40.0
SECTOR_HALF_WIDTH_DEG = 50.0
BIN_COUNT = 100
MAX_GAP_FILL_DEG = 2
EARTH_KM_PER_DEG = 111.0


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def rain_footprint_id(observed_at: datetime) -> str:
    return "mrms-footprint-" + observed_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def rate_tiers(rates: np.ndarray) -> np.ndarray:
    values = np.asarray(rates, dtype=float)
    tiers = np.zeros(values.shape, dtype=np.uint8)
    finite = np.isfinite(values)
    tiers[finite & (values >= RATE_TIERS_MM_HR[0])] = 1
    tiers[finite & (values >= RATE_TIERS_MM_HR[1])] = 2
    tiers[finite & (values >= RATE_TIERS_MM_HR[2])] = 3
    return tiers


def encode_runs(tiers: np.ndarray) -> list[list[int]]:
    """Losslessly encode nonzero tier cells as deterministic row runs."""
    tiers = np.asarray(tiers, dtype=np.uint8)
    if tiers.ndim != 2:
        raise ValueError("Rain tiers must be a two-dimensional grid")
    runs: list[list[int]] = []
    for row_index, row in enumerate(tiers):
        occupied = np.flatnonzero(row)
        if not occupied.size:
            continue
        start = previous = int(occupied[0])
        tier = int(row[start])
        for raw_column in occupied[1:]:
            column = int(raw_column)
            next_tier = int(row[column])
            if column != previous + 1 or next_tier != tier:
                runs.append([row_index, start, previous, tier])
                start, tier = column, next_tier
            previous = column
        runs.append([row_index, start, previous, tier])
    return runs


def display_wet_runs(rates: np.ndarray) -> list[list[int]]:
    """Losslessly retain the map's exact 0.2 mm/hr display threshold."""
    values = np.asarray(rates, dtype=float)
    return encode_runs((np.isfinite(values) & (values >= DISPLAY_RAIN_THRESHOLD_MM_HR)).astype(np.uint8))


def regular_step(values: np.ndarray, name: str) -> float:
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or values.size < 2:
        raise ValueError(f"{name} coordinates must be a one-dimensional regular grid")
    steps = np.diff(values)
    # GRIB coordinate expansion introduces roughly 1e-6-degree jitter into
    # the otherwise regular 0.01-degree MRMS latitude axis.
    step = float(np.median(steps))
    tolerance = max(2e-6, abs(step) * 1e-4)
    if not np.allclose(steps, step, rtol=0, atol=tolerance):
        raise ValueError(f"{name} coordinates are not regularly spaced")
    return round(step, 10)


def build_sidecar(latitudes: np.ndarray, longitudes: np.ndarray, rates: np.ndarray, observed_at: datetime, source_key: str) -> dict:
    latitudes = np.asarray(latitudes, dtype=float)
    longitudes = np.asarray(longitudes, dtype=float)
    rates = np.asarray(rates, dtype=float)
    if rates.shape != (latitudes.size, longitudes.size):
        raise ValueError("MRMS rate grid must match its coordinates")
    tiers = rate_tiers(rates)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "rainFootprintId": rain_footprint_id(observed_at),
        "observedAt": iso_utc(observed_at),
        "sourceKey": source_key,
        "grid": {
            "latitudeCount": int(latitudes.size), "longitudeCount": int(longitudes.size),
            "latitudeStart": float(latitudes[0]), "latitudeStepDeg": regular_step(latitudes, "latitude"),
            "longitudeStart": float(longitudes[0]), "longitudeStepDeg": regular_step(longitudes, "longitude"),
        },
        "rateTiersMmHr": list(RATE_TIERS_MM_HR),
        "runs": encode_runs(tiers),
        "stormSegmentation": {
            "version": STORM_SEGMENTATION_VERSION,
            "envelopeMinimumMmHr": STORM_ENVELOPE_THRESHOLD_MM_HR,
            "coreMinimumMmHr": STORM_CORE_THRESHOLD_MM_HR,
            "envelopeRuns": encode_runs(
                (np.isfinite(rates) & (rates >= STORM_ENVELOPE_THRESHOLD_MM_HR)).astype(np.uint8)
            ),
            "coreRuns": encode_runs(
                (np.isfinite(rates) & (rates >= STORM_CORE_THRESHOLD_MM_HR)).astype(np.uint8)
            ),
        },
        "displayRainThresholdMmHr": DISPLAY_RAIN_THRESHOLD_MM_HR,
        "displayWetRuns": display_wet_runs(rates),
    }


def build_storm_segmentation_sidecar(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    rates: np.ndarray,
    observed_at: datetime,
    source_key: str,
) -> dict:
    """Build only the exact hysteresis inputs needed by storm-object replay."""
    latitudes = np.asarray(latitudes, dtype=float)
    longitudes = np.asarray(longitudes, dtype=float)
    rates = np.asarray(rates, dtype=float)
    if rates.shape != (latitudes.size, longitudes.size):
        raise ValueError("MRMS rate grid must match its coordinates")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "rainFootprintId": rain_footprint_id(observed_at),
        "observedAt": iso_utc(observed_at),
        "sourceKey": source_key,
        "grid": {
            "latitudeCount": int(latitudes.size),
            "longitudeCount": int(longitudes.size),
            "latitudeStart": float(latitudes[0]),
            "latitudeStepDeg": regular_step(latitudes, "latitude"),
            "longitudeStart": float(longitudes[0]),
            "longitudeStepDeg": regular_step(longitudes, "longitude"),
        },
        "stormSegmentation": {
            "version": STORM_SEGMENTATION_VERSION,
            "envelopeMinimumMmHr": STORM_ENVELOPE_THRESHOLD_MM_HR,
            "coreMinimumMmHr": STORM_CORE_THRESHOLD_MM_HR,
            "envelopeRuns": encode_runs(
                (np.isfinite(rates) & (rates >= STORM_ENVELOPE_THRESHOLD_MM_HR)).astype(np.uint8)
            ),
            "coreRuns": encode_runs(
                (np.isfinite(rates) & (rates >= STORM_CORE_THRESHOLD_MM_HR)).astype(np.uint8)
            ),
        },
    }


def fill_small_internal_gaps(occupied: np.ndarray, maximum_gap: int = MAX_GAP_FILL_DEG) -> np.ndarray:
    filled = np.asarray(occupied, dtype=bool).copy()
    true_indices = np.flatnonzero(filled)
    for left, right in zip(true_indices, true_indices[1:]):
        gap = int(right - left - 1)
        if 0 < gap <= maximum_gap:
            filled[left + 1:right] = True
    return filled


def occupancy_metrics(occupied: np.ndarray) -> dict:
    occupied = fill_small_internal_gaps(occupied)
    indices = np.flatnonzero(occupied)
    if not indices.size:
        return {"arcSpanDeg": 0, "occupiedDeg": 0, "segmentCount": 0}
    breaks = np.flatnonzero(np.diff(indices) > 1)
    starts = np.r_[0, breaks + 1]
    ends = np.r_[breaks, indices.size - 1]
    widths = indices[ends] - indices[starts] + 1
    return {"arcSpanDeg": int(widths.max()), "occupiedDeg": int(occupied.sum()), "segmentCount": int(len(starts))}


def candidate_rain_span(candidate: dict, latitudes: np.ndarray, longitudes: np.ndarray, rates: np.ndarray) -> dict:
    """Measure radar-backed azimuth occupancy around a candidate's anti-solar sector."""
    observer_lat, observer_lon = float(candidate["lat"]), float(candidate["lon"])
    anti_solar = float(candidate["antiSolarBearingDeg"])
    latitudes, longitudes, rates = np.asarray(latitudes, dtype=float), np.asarray(longitudes, dtype=float), np.asarray(rates, dtype=float)
    latitude_margin = MAX_DISTANCE_KM / EARTH_KM_PER_DEG
    longitude_scale = max(0.2, math.cos(math.radians(observer_lat)))
    longitude_margin = MAX_DISTANCE_KM / (EARTH_KM_PER_DEG * longitude_scale)
    row_indices = np.flatnonzero(np.abs(latitudes - observer_lat) <= latitude_margin)
    column_indices = np.flatnonzero(np.abs(longitudes - observer_lon) <= longitude_margin)
    empty = occupancy_metrics(np.zeros(BIN_COUNT, dtype=bool))
    if not row_indices.size or not column_indices.size:
        return {"any": empty, "tier2Plus": dict(empty), "tier3": dict(empty)}
    local_rates = rates[np.ix_(row_indices, column_indices)]
    local_tiers = rate_tiers(local_rates)
    north_km = (latitudes[row_indices][:, None] - observer_lat) * EARTH_KM_PER_DEG
    east_km = (longitudes[column_indices][None, :] - observer_lon) * EARTH_KM_PER_DEG * longitude_scale
    distance = np.hypot(north_km, east_km)
    bearing = (np.degrees(np.arctan2(east_km, north_km)) + 360.0) % 360.0
    relative = (bearing - anti_solar + 180.0) % 360.0 - 180.0
    sector = ((distance >= MIN_DISTANCE_KM) & (distance <= MAX_DISTANCE_KM) & (relative >= -SECTOR_HALF_WIDTH_DEG) & (relative <= SECTOR_HALF_WIDTH_DEG))
    bins = np.clip(np.floor(relative + SECTOR_HALF_WIDTH_DEG).astype(int), 0, BIN_COUNT - 1)
    result = {}
    for name, minimum_tier in (("any", 1), ("tier2Plus", 2), ("tier3", 3)):
        occupied = np.zeros(BIN_COUNT, dtype=bool)
        selected_bins = bins[sector & (local_tiers >= minimum_tier)]
        if selected_bins.size:
            occupied[np.unique(selected_bins)] = True
        result[name] = occupancy_metrics(occupied)
    return result


def attach_candidate_rain_spans(candidates: list[dict], latitudes: np.ndarray, longitudes: np.ndarray, rates: np.ndarray) -> None:
    for candidate in candidates:
        by_tier = candidate_rain_span(candidate, latitudes, longitudes, rates)
        any_rain = by_tier["any"]
        candidate["antiSolarRainArcSpanDeg"] = any_rain["arcSpanDeg"]
        candidate["antiSolarRainOccupiedDeg"] = any_rain["occupiedDeg"]
        candidate["antiSolarRainSegmentCount"] = any_rain["segmentCount"]
        candidate["antiSolarRainSpanByTier"] = by_tier


def attach_record_rain_spans(records: list[dict], candidates: list[dict], latitudes: np.ndarray, longitudes: np.ndarray, rates: np.ndarray, radar_observed_at: str | None = None) -> None:
    """Calculate spans after live publication and merge them into decision records."""
    from decision_log import candidate_id

    attach_candidate_rain_spans(candidates, latitudes, longitudes, rates)
    spans = {candidate_id(item, radar_observed_at): item for item in candidates}
    for record in records:
        features = record.get("features") or {}
        rain = features.get("rain") or {}
        candidate = spans.get(record.get("candidateId"))
        if not candidate:
            continue
        rain["antiSolarRainArcSpanDeg"] = candidate["antiSolarRainArcSpanDeg"]
        rain["antiSolarRainOccupiedDeg"] = candidate["antiSolarRainOccupiedDeg"]
        rain["antiSolarRainSegmentCount"] = candidate["antiSolarRainSegmentCount"]
        rain["antiSolarRainSpanByTier"] = candidate["antiSolarRainSpanByTier"]
