"""DNI and GOES enrichment for the MRMS observer shortlist."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from sunlight_decision import build_decision, collect_sources
from decision_log import build_record, prefilter_reasons

DIRECT_GO_MIN = 200
DIRECT_WATCH_MIN = 120
OPTIMAL_SUN_MIN = 5
OPTIMAL_SUN_MAX = 22
WATCH_SUN_MIN = 0
WATCH_SUN_MAX = 30
GEOMETRY_FALLBACK_RADAR_MIN = 90
GEOMETRY_FALLBACK_RAIN_DISTANCE_MAX_KM = 15
GEOMETRY_FALLBACK_OBSERVER_RAIN_MAX_MM_HR = 0.05
GEOMETRY_FALLBACK_REASON = 'strong-radar-geometry-sunlight-uncertain'
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "RainbowConnectorWorker/0.1"


def decision_constants() -> dict:
    return {
        "direct_watch_min": DIRECT_WATCH_MIN,
        "direct_go_min": DIRECT_GO_MIN,
        "fallback_radar_min": GEOMETRY_FALLBACK_RADAR_MIN,
        "fallback_rain_distance_max": GEOMETRY_FALLBACK_RAIN_DISTANCE_MAX_KM,
        "fallback_observer_rain_max": GEOMETRY_FALLBACK_OBSERVER_RAIN_MAX_MM_HR,
        "optimal_sun_min": OPTIMAL_SUN_MIN,
        "optimal_sun_max": OPTIMAL_SUN_MAX,
    }


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def batch_open_meteo(
    candidates: list[dict],
    session: requests.Session | None = None,
    batch_size: int = 10,
) -> list[dict]:
    """Fetch current instantaneous DNI for a reduced candidate list."""
    session = session or requests.Session()
    enriched: list[dict] = []
    batch_size = max(1, min(10, batch_size))
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        response = None
        try:
            response = session.get(
                OPEN_METEO_URL,
                params={
                    "latitude": ",".join(f"{item['lat']:.4f}" for item in batch),
                    "longitude": ",".join(f"{item['lon']:.4f}" for item in batch),
                    "current": "cloud_cover,direct_normal_irradiance_instant",
                    "forecast_days": 1,
                    "timezone": "GMT",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            rows = body if isinstance(body, list) else [body]
            if len(rows) != len(batch):
                raise RuntimeError(f"Open-Meteo returned {len(rows)} rows for {len(batch)} candidates")
        except Exception as exc:
            status = getattr(response, "status_code", None)
            reason = f"Open-Meteo batch failed ({status})" if status else f"Open-Meteo batch failed: {exc}"
            enriched.extend({
                **candidate,
                "dniWm2": None,
                "cloudCoverPct": None,
                "sunlightObservedAt": None,
                "sunlightError": reason,
            } for candidate in batch)
            continue
        for candidate, row in zip(batch, rows):
            current = row.get("current") or {}
            enriched.append({
                **candidate,
                "dniWm2": current.get("direct_normal_irradiance_instant"),
                "cloudCoverPct": current.get("cloud_cover"),
                "sunlightObservedAt": current.get("time"),
            })
    return enriched


def compact_goes_source(source: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "available", "fresh", "positive", "negative", "strength", "reason",
        "observedAt", "observedAgeMinutes", "maxAgeMinutes", "product",
        "variable", "satellite", "satellitePosition", "value", "units", "s3Key",
        "dqf", "dqfMeaning", "quantitativeSolarZenithBoundsDeg", "retrievalSolarZenithBoundsDeg",
    )
    return {name: source.get(name) for name in keep if name in source}


def sample_goes(candidate: dict, cache_dir: Path) -> tuple[dict, dict]:
    sources = collect_sources(
        lat=candidate["lat"],
        lon=candidate["lon"],
        satellite="auto",
        hours_back=3,
        cache_dir=cache_dir,
        open_meteo=False,
    )
    return build_decision(sources), sources


def finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def strong_geometry_fallback(candidate: dict) -> bool:
    radar_score = finite(candidate.get('radarScore'))
    elevation = finite(candidate.get('sunElevationDeg'))
    rain_distance = finite(candidate.get('rainDistanceKm'))
    observer_rain = finite(candidate.get('observerRainRateMmHr'))
    return (
        radar_score is not None and radar_score >= GEOMETRY_FALLBACK_RADAR_MIN
        and elevation is not None and OPTIMAL_SUN_MIN <= elevation <= OPTIMAL_SUN_MAX
        and rain_distance is not None and rain_distance <= GEOMETRY_FALLBACK_RAIN_DISTANCE_MAX_KM
        and observer_rain is not None and observer_rain <= GEOMETRY_FALLBACK_OBSERVER_RAIN_MAX_MM_HR
    )


def materialize(
    candidate: dict,
    verdict: str,
    rank: int,
    goes: dict,
    sources: dict,
    selection_reason: str | None = None,
) -> dict:
    dni = finite(candidate.get("dniWm2"))
    radar_score = finite(candidate.get("radarScore")) or 0
    dni_score = max(0, min(100, ((dni or 0) - DIRECT_WATCH_MIN) / 2.8))
    goes_score = 100 if goes.get("sunlightDecision") == "go" else 45 if goes.get("sunlightDecision") == "watch" else 0
    score = round(0.55 * radar_score + 0.30 * dni_score + 0.15 * goes_score, 1)
    return {
        "id": f"{candidate.get('rainLat')},{candidate.get('rainLon')}:{rank}",
        "rank": rank,
        "lat": candidate["lat"],
        "lon": candidate["lon"],
        "label": f"{candidate['lat']:.2f}, {candidate['lon']:.2f}",
        "verdict": verdict,
        "confidence": "high" if verdict == "go" else "possible",
        "direction": {
            "bearing": round(candidate["antiSolarBearingDeg"]),
            "label": f"Look toward {round(candidate['antiSolarBearingDeg'])} degrees",
        },
        "sunlightDecision": goes.get("sunlightDecision"),
        "sunlightConfidence": goes.get("confidence"),
        "evidence": {
            "sunElevationDeg": candidate["sunElevationDeg"],
            "directNormalIrradianceWm2": None if dni is None else round(dni),
            "cloudCoverPct": candidate.get("cloudCoverPct"),
            "rainRateMmHr": candidate["rainRateMmHr"],
            "rainIntensity": min(1, candidate["rainRateMmHr"] / 2),
            "observerRainRateMmHr": candidate["observerRainRateMmHr"],
            "observerRainIntensity": min(1, candidate["observerRainRateMmHr"] / 2),
            "spatialSupport": candidate.get("spatialSupport"),
            "rainPoint": {
                "lat": candidate["rainLat"],
                "lon": candidate["rainLon"],
                "distanceKm": candidate["rainDistanceKm"],
                "bearing": round(candidate["antiSolarBearingDeg"]),
            },
            "score": score,
            "radarScore": radar_score,
            "selectionReason": selection_reason,
            "goes": {
                "decision": goes.get("sunlightDecision"),
                "positiveSources": goes.get("positiveSources", []),
                "negativeSources": goes.get("negativeSources", []),
                "sources": {name: compact_goes_source(value) for name, value in sources.items()},
            },
            "calibration": "FAA+ARM-2026-07",
        },
    }


def oldest_time(values: list[str]) -> str | None:
    parsed = [parse_utc(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    return iso_utc(min(parsed)) if parsed else None


def enrich_shortlist(
    radar_artifact: dict,
    cache_dir: Path,
    session: requests.Session | None = None,
    decision_records: list[dict] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    shortlist = radar_artifact.get("shortlist") or []
    weather = batch_open_meteo(shortlist, session=session)
    viable = []
    geometry_fallback = []
    decisions = []
    constants = decision_constants()
    radar_observed_at = ((radar_artifact.get("sourceHealth") or {}).get("radar") or {}).get("observedAt")
    for candidate in weather:
        dni = finite(candidate.get("dniWm2"))
        cloud = finite(candidate.get("cloudCoverPct"))
        uniform_overcast = cloud is not None and cloud >= 90 and (dni is None or dni < DIRECT_WATCH_MIN)
        if dni is not None and dni >= DIRECT_WATCH_MIN and not uniform_overcast:
            viable.append(candidate)
        elif strong_geometry_fallback(candidate):
            geometry_fallback.append(candidate)
        else:
            decisions.append(build_record(
                candidate, radar_observed_at, "rejected", "model_prefilter",
                prefilter_reasons(candidate, constants), constants,
            ))

    go_candidates: list[dict] = []
    possible_candidates: list[dict] = []
    goes_cloud_times: list[str] = []
    goes_irradiance_times: list[str] = []
    errors: list[dict] = []
    ranked = []
    checked = [*viable, *geometry_fallback]
    fallback_ids = {id(candidate) for candidate in geometry_fallback}
    for candidate in checked:
        try:
            decision, sources = sample_goes(candidate, cache_dir)
            acmc_time = (sources.get("goesAcmc") or {}).get("observedAt")
            dsrf_time = (sources.get("goesDsrf") or {}).get("observedAt")
            if acmc_time:
                goes_cloud_times.append(acmc_time)
            if dsrf_time:
                goes_irradiance_times.append(dsrf_time)
            elevation = candidate["sunElevationDeg"]
            dni = finite(candidate.get("dniWm2")) or 0
            strict_go = (
                OPTIMAL_SUN_MIN <= elevation <= OPTIMAL_SUN_MAX
                and dni >= DIRECT_GO_MIN
                and decision.get("sunlightDecision") == "go"
            )
            possible = (
                WATCH_SUN_MIN <= elevation <= WATCH_SUN_MAX
                and dni >= DIRECT_WATCH_MIN
                and decision.get("sunlightDecision") in ("go", "watch")
            )
            fallback = id(candidate) in fallback_ids and not strict_go and not possible
            if strict_go or possible or fallback:
                reason = "selected_strict_go" if strict_go else (
                    "selected_geometry_fallback_v1" if fallback else "selected_model_possible"
                )
                decisions.append(build_record(
                    candidate, radar_observed_at,
                    "selected_go" if strict_go else "selected_possible",
                    "final_selection", [reason], constants, decision, sources,
                ))
                ranked.append((
                    candidate,
                    "go" if strict_go else "watch",
                    decision,
                    sources,
                    GEOMETRY_FALLBACK_REASON if fallback else None,
                ))
            else:
                reason = "satellite_unknown" if decision.get("sunlightDecision") == "unknown" else "satellite_blocked"
                decisions.append(build_record(
                    candidate, radar_observed_at, "rejected", "satellite_corroboration",
                    [reason], constants, decision, sources,
                ))
        except Exception as exc:
            errors.append({"lat": candidate["lat"], "lon": candidate["lon"], "error": str(exc)})
            decisions.append(build_record(
                candidate, radar_observed_at, "rejected", "satellite_corroboration",
                ["satellite_sampling_error"], constants,
            ))

    ranked.sort(key=lambda item: item[0].get("radarScore", 0), reverse=True)
    for candidate, verdict, decision, sources, selection_reason in ranked:
        target = go_candidates if verdict == "go" else possible_candidates
        target.append(materialize(
            candidate, verdict, len(target) + 1, decision, sources, selection_reason,
        ))

    model_times = [item["sunlightObservedAt"] for item in weather if item.get("sunlightObservedAt")]
    model_errors = [item["sunlightError"] for item in weather if item.get("sunlightError")]
    if decision_records is not None:
        decision_records.extend(decisions)
    expires_at = now + timedelta(minutes=7)
    source_health = {
        **(radar_artifact.get("sourceHealth") or {}),
        "goesCloud": {
            "provider": "NOAA GOES-19/18 ACMC",
            "observedAt": oldest_time(goes_cloud_times),
            "maxAgeMinutes": 20,
            "required": bool(checked),
        },
        "goesIrradiance": {
            "provider": "NOAA GOES-19/18 DSRF",
            "observedAt": oldest_time(goes_irradiance_times),
            "maxAgeMinutes": 30,
            "required": bool(checked),
        },
        "sunlightModel": {
            "provider": "Open-Meteo instantaneous DNI",
            "observedAt": oldest_time(model_times),
            "maxAgeMinutes": 30,
            "required": bool(shortlist),
        },
        "surfaceObservation": {"provider": "NWS/METAR", "required": False},
    }
    return {
        "schemaVersion": 3,
        "dataStatus": "live",
        "generatedAt": iso_utc(now),
        "expiresAt": iso_utc(expires_at),
        "cadenceMinutes": 5,
        "source": {
            "radar": "NOAA MRMS PrecipRate",
            "sunlight": "Open-Meteo DNI instant + GOES DSRF/ACMC corroboration",
        },
        "sourceHealth": source_health,
        "criteria": {
            "observerCoverage": "physical land within the contiguous United States",
            "highConfidenceSunElevation": "5-22 degrees",
            "possibleSunElevation": "0-30 degrees",
            "directSun": "GO requires instantaneous DNI >= 200 W/m2 and GOES corroboration",
            "rainGeometry": "observer is dry and sunward of an MRMS precipitation edge",
        },
        "candidates": go_candidates,
        "possibleCandidates": possible_candidates,
        "diagnostics": {
            **(radar_artifact.get("diagnostics") or {}),
            "dniChecked": len(weather),
            "dniErrors": len(model_errors),
            "dniErrorReasons": sorted(set(model_errors)),
            "dniViable": len(viable),
            "strongGeometryFallbackChecked": len(geometry_fallback),
            "goesChecked": len(checked),
            "goesErrors": errors,
            "goCandidatesBeforePersistence": len(go_candidates),
            "possibleCandidates": len(possible_candidates),
        },
    }
