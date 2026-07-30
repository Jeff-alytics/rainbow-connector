#!/usr/bin/env python
"""
Build a sunlight decision from multiple sources.

Hierarchy:
1. GOES DSRF downward shortwave radiation: best direct signal.
2. GOES ACMC clear sky mask: fastest satellite cloud signal.
3. Open-Meteo direct radiation/cloud cover: model fallback/evidence.

This is still a prototype. It intentionally outputs all source evidence so the
rainbow detector can later distinguish "go" from "mixed-go".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import requests

from goes_sample import CACHE_DIR, DEFAULT_PRODUCT, FALLBACK_PRODUCT, sample


DSRF_MAX_AGE_MIN = 30
ACMC_MAX_AGE_MIN = 20
OM_DIRECT_MIN = 200
OM_CLOUD_BLOCK_MIN = 75
OM_DIRECT_BLOCK_MAX = 50


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a hierarchical sunlight decision for a point.")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--satellite", choices=["auto", "east", "west"], default="auto")
    p.add_argument("--hours-back", type=int, default=6)
    p.add_argument("--cache-dir", default=str(CACHE_DIR))
    p.add_argument("--no-open-meteo", action="store_true")
    return p.parse_args()


def ok_age(source: dict[str, Any], max_age_min: int) -> bool:
    age = source.get("observedAgeMinutes")
    return age is not None and age <= max_age_min


def source_error(name: str, err: Exception) -> dict[str, Any]:
    return {
        "name": name,
        "available": False,
        "positive": None,
        "negative": None,
        "strength": "unavailable",
        "reason": str(err),
    }


def dsrf_source(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("value")
    fresh = ok_age(raw, DSRF_MAX_AGE_MIN)
    positive = bool(fresh and value is not None and float(value) >= OM_DIRECT_MIN)
    negative = bool(fresh and value is not None and float(value) < OM_DIRECT_BLOCK_MAX)
    strength = "unknown"
    if positive:
        strength = "strong-context" if float(value) >= 300 else "moderate-context"
    elif negative:
        strength = "blocked"
    elif not fresh:
        strength = "stale"
    elif value is not None:
        strength = "weak"
    return {
        **raw,
        "name": "goesDsrf",
        "available": True,
        "fresh": fresh,
        "maxAgeMinutes": DSRF_MAX_AGE_MIN,
        "positive": positive,
        "negative": negative,
        "strength": strength,
        "reason": f"downward shortwave flux {value} {raw.get('units')} (not direct-normal irradiance)" if value is not None else "DSR missing",
    }


def acmc_source(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("value")
    fresh = ok_age(raw, ACMC_MAX_AGE_MIN)
    positive = bool(fresh and value == 0)
    negative = bool(fresh and value == 1)
    strength = "unknown"
    if positive:
        strength = "clear-mask"
    elif negative:
        strength = "cloudy-mask"
    elif not fresh:
        strength = "stale"
    return {
        **raw,
        "name": "goesAcmc",
        "available": True,
        "fresh": fresh,
        "maxAgeMinutes": ACMC_MAX_AGE_MIN,
        "positive": positive,
        "negative": negative,
        "strength": strength,
        "reason": "clear/probably clear" if value == 0 else "cloudy/probably cloudy" if value == 1 else "mask unknown",
    }


def fetch_open_meteo(lat: float, lon: float) -> dict[str, Any]:
    url = "https://api.open-meteo.com/v1/forecast"
    r = requests.get(
        url,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "cloud_cover,direct_normal_irradiance_instant",
            "forecast_days": 1,
            "timezone": "GMT",
        },
        timeout=12,
    )
    r.raise_for_status()
    current = r.json().get("current") or {}
    cloud = current.get("cloud_cover")
    direct = current.get("direct_normal_irradiance_instant")
    direct_yes = direct is not None and float(direct) >= OM_DIRECT_MIN
    negative = (
        direct is not None
        and cloud is not None
        and float(direct) < OM_DIRECT_BLOCK_MAX
        and float(cloud) >= OM_CLOUD_BLOCK_MIN
    )
    positive = bool(direct_yes)
    return {
        "name": "openMeteo",
        "available": True,
        "positive": positive,
        "negative": bool(negative),
        "strength": "direct-normal-irradiance" if direct_yes else "blocked" if negative else "unknown",
        "cloudCoverPct": cloud,
        "directNormalIrradianceWm2": direct,
        "thresholds": {
            "directGoMinWm2": OM_DIRECT_MIN,
            "directBlockMaxWm2": OM_DIRECT_BLOCK_MAX,
            "cloudBlockMinPct": OM_CLOUD_BLOCK_MIN,
        },
    }


def collect_sources(lat: float, lon: float, satellite: str, hours_back: int, cache_dir: Path, open_meteo: bool) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    try:
        sources["goesDsrf"] = dsrf_source(sample(lat, lon, DEFAULT_PRODUCT, satellite, hours_back, cache_dir))
    except Exception as err:
        sources["goesDsrf"] = source_error("goesDsrf", err)

    try:
        sources["goesAcmc"] = acmc_source(sample(lat, lon, FALLBACK_PRODUCT, satellite, hours_back, cache_dir))
    except Exception as err:
        sources["goesAcmc"] = source_error("goesAcmc", err)

    if open_meteo:
        try:
            sources["openMeteo"] = fetch_open_meteo(lat, lon)
        except Exception as err:
            sources["openMeteo"] = source_error("openMeteo", err)
    return sources


def build_decision(sources: dict[str, Any]) -> dict[str, Any]:
    positive = [name for name, source in sources.items() if source.get("positive") is True]
    negative = [name for name, source in sources.items() if source.get("negative") is True]

    # DSRF includes diffuse light and cannot establish a visible solar disc by
    # itself. Treat satellite sunlight as corroborated only when fresh DSRF and
    # the clear-sky mask agree. Actual direct-normal irradiance remains the best
    # standalone signal.
    satellite_pair = "goesDsrf" in positive and "goesAcmc" in positive
    model_yes = [name for name in positive if name == "openMeteo"]
    satellite_no = [name for name in negative if name in ("goesDsrf", "goesAcmc")]

    if model_yes:
        decision = "mixed-go" if satellite_no else "go"
    elif satellite_pair:
        decision = "go"
    elif "goesDsrf" in positive or "goesAcmc" in positive:
        decision = "watch"
    elif "goesDsrf" in negative and "goesAcmc" in negative:
        decision = "blocked"
    elif negative:
        decision = "watch"
    else:
        decision = "unknown"

    if decision == "go":
        confidence = "strong" if satellite_pair else "moderate"
    elif decision == "mixed-go":
        confidence = "mixed"
    elif decision == "blocked":
        confidence = "strong-block"
    else:
        confidence = "low"

    return {
        "sunlightDecision": decision,
        "sunlit": decision in ("go", "mixed-go"),
        "confidence": confidence,
        "positiveSources": positive,
        "negativeSources": negative,
        "goSources": (["goesDsrf", "goesAcmc"] if satellite_pair else model_yes),
        "conflictSources": satellite_no,
    }


def main() -> int:
    args = parse_args()
    cache_dir = Path(args.cache_dir)
    sources = collect_sources(
        lat=args.lat,
        lon=args.lon,
        satellite=args.satellite,
        hours_back=args.hours_back,
        cache_dir=cache_dir,
        open_meteo=not args.no_open_meteo,
    )
    decision = build_decision(sources)
    print(json.dumps({"lat": args.lat, "lon": args.lon, **decision, "sources": sources}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
