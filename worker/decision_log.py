"""Private, replayable detector decision records."""

from __future__ import annotations

import hashlib
import math
from typing import Any

RULE_VERSION = "noaa-detector-2026-07-v1"
SUNLIGHT_V2_METHOD_VERSION = "sunlight-v2-shadow-2026-08-v4"


def finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def candidate_id(candidate: dict, radar_observed_at: str | None) -> str:
    def coordinate(name: str) -> str:
        value = finite(candidate.get(name))
        return "unknown" if value is None else f"{value:.4f}"

    parts = (
        radar_observed_at or "unknown",
        coordinate("lat"), coordinate("lon"),
        coordinate("rainLat"), coordinate("rainLon"),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def source_feature(source: dict | None, value_name: str = "value") -> dict:
    source = source or {}
    return {value_name: source.get("value"), "observedAt": source.get("observedAt"), "fresh": source.get("fresh"), "available": source.get("available"), "error": source.get("error")}


def dsrf_feature(source: dict | None, sun_elevation_deg: Any) -> dict:
    source = source or {}
    feature = source_feature(source, "valueWm2")
    elevation = finite(sun_elevation_deg)
    solar_zenith = None if elevation is None else 90 - elevation
    dqf = finite(source.get("dqf"))
    bounds = source.get("quantitativeSolarZenithBoundsDeg")
    valid_bounds = (
        isinstance(bounds, (list, tuple)) and len(bounds) == 2
        and finite(bounds[0]) is not None and finite(bounds[1]) is not None
    )
    usable = None
    reason = "quality_metadata_unavailable"
    if dqf is not None and solar_zenith is not None and valid_bounds:
        low, high = float(bounds[0]), float(bounds[1])
        if not low <= solar_zenith <= high:
            usable, reason = False, "outside_quantitative_solar_zenith_range"
        elif int(dqf) != 0:
            usable, reason = False, "dqf_degraded_or_invalid"
        else:
            usable, reason = True, None
    feature.update({
        "dqf": None if dqf is None else int(dqf),
        "dqfMeaning": source.get("dqfMeaning"),
        "solarZenithDeg": None if solar_zenith is None else round(solar_zenith, 4),
        "quantitativeSolarZenithBoundsDeg": list(bounds) if valid_bounds else None,
        "retrievalSolarZenithBoundsDeg": source.get("retrievalSolarZenithBoundsDeg"),
        "usable": usable,
        "unavailableReason": reason,
    })
    return feature


def threshold_snapshot(constants: dict) -> dict:
    return {
        "directWatchMinWm2": constants["direct_watch_min"], "directGoMinWm2": constants["direct_go_min"],
        "geometryFallbackRadarMin": constants["fallback_radar_min"],
        "geometryFallbackRainDistanceMaxKm": constants["fallback_rain_distance_max"],
        "geometryFallbackObserverRainMaxMmHr": constants["fallback_observer_rain_max"],
        "optimalSunMinDeg": constants["optimal_sun_min"], "optimalSunMaxDeg": constants["optimal_sun_max"],
    }


def build_record(candidate: dict, radar_observed_at: str | None, disposition: str, decision_stage: str, decision_reasons: list[str], constants: dict, goes: dict | None = None, sources: dict | None = None) -> dict:
    sources = sources or {}
    return {
        "candidateId": candidate_id(candidate, radar_observed_at), "disposition": disposition,
        "decisionStage": decision_stage, "decisionReasons": list(dict.fromkeys(decision_reasons)),
        "features": {
            "observer": {"lat": candidate.get("lat"), "lon": candidate.get("lon")},
            "rain": {
                "lat": candidate.get("rainLat"), "lon": candidate.get("rainLon"),
                "distanceKm": finite(candidate.get("rainDistanceKm")), "rateMmHr": finite(candidate.get("rainRateMmHr")),
                "observerRateMmHr": finite(candidate.get("observerRainRateMmHr")),
                "spatialSupport": candidate.get("spatialSupport"),
                "antiSolarRainArcSpanDeg": candidate.get("antiSolarRainArcSpanDeg"),
                "antiSolarRainOccupiedDeg": candidate.get("antiSolarRainOccupiedDeg"),
                "antiSolarRainSegmentCount": candidate.get("antiSolarRainSegmentCount"),
                "antiSolarRainSpanByTier": candidate.get("antiSolarRainSpanByTier"),
            },
            "geometry": {
                "sunElevationDeg": finite(candidate.get("sunElevationDeg")), "sunBearingDeg": finite(candidate.get("sunBearingDeg")),
                "antiSolarBearingDeg": finite(candidate.get("antiSolarBearingDeg")), "radarScore": finite(candidate.get("radarScore")),
            },
            "model": {
                "directNormalIrradianceWm2": finite(candidate.get("dniWm2")), "cloudCoverPct": finite(candidate.get("cloudCoverPct")),
                "observedAt": candidate.get("sunlightObservedAt"), "error": candidate.get("sunlightError"),
            },
            "satellite": {
                "decision": (goes or {}).get("sunlightDecision"), "acmc": source_feature(sources.get("goesAcmc")),
                "dsrf": dsrf_feature(sources.get("goesDsrf"), candidate.get("sunElevationDeg")), "clearSkyExpectedDsrfWm2": None,
                "dsrfClearnessRatio": None, "clearSkyMethodVersion": None,
            },
            "persistence": {"priorMatchingScans": candidate.get("priorMatchingScans", 0)},
            "sunlightV2": None,
            "thresholdSnapshot": threshold_snapshot(constants),
        },
    }


def prefilter_reasons(candidate: dict, constants: dict) -> list[str]:
    reasons = []
    dni, cloud = finite(candidate.get("dniWm2")), finite(candidate.get("cloudCoverPct"))
    radar, elevation = finite(candidate.get("radarScore")), finite(candidate.get("sunElevationDeg"))
    rain_distance, observer_rain = finite(candidate.get("rainDistanceKm")), finite(candidate.get("observerRainRateMmHr"))
    if dni is None: reasons.append("model_data_unavailable")
    elif dni < constants["direct_watch_min"]: reasons.append("model_dni_below_watch")
    if cloud is not None and cloud >= 90 and (dni is None or dni < constants["direct_watch_min"]): reasons.append("model_uniform_overcast")
    if radar is None or radar < constants["fallback_radar_min"]: reasons.append("geometry_fallback_radar_below_min")
    if rain_distance is None or rain_distance > constants["fallback_rain_distance_max"]: reasons.append("geometry_fallback_rain_too_distant")
    if elevation is None or not constants["optimal_sun_min"] <= elevation <= constants["optimal_sun_max"]: reasons.append("geometry_fallback_sun_outside_band")
    if observer_rain is None or observer_rain > constants["fallback_observer_rain_max"]: reasons.append("geometry_fallback_observer_not_dry")
    return reasons or ["final_policy_rejected"]
