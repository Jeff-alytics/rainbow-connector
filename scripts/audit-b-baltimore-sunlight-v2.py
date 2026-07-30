#!/usr/bin/env python
"""Replay sunlight-v2 at Baltimore's corrected confirmed-bow window."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "api"))

from detector_core import solar_position  # noqa: E402
from goes_sample import DEFAULT_PRODUCT, FALLBACK_PRODUCT, sample  # noqa: E402
from sunlight_v2 import (  # noqa: E402
    METHOD_VERSION, Band2Sampler, acmc_neighborhood, cleanup_shadow_cache,
    combine_shadow, dsrf_shadow,
)

AUDIT_DIR = ROOT / "validation" / "audit-b"
REPLAY = AUDIT_DIR / "baltimore-mrms-replay-v1.json"
OUTPUT = AUDIT_DIR / "baltimore-sunlight-v2-replay.json"
CACHE = ROOT / ".worker-cache" / "baltimore-sunlight-v2"
TIMES = ("2026-07-28T23:30:00Z", "2026-07-28T23:40:00Z")


def canonical_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def shadow_category(probability: float | None) -> str:
    if probability is None:
        return "unknown"
    if probability >= 0.65:
        return "sunlit"
    if probability >= 0.4:
        return "uncertain"
    return "dark"


def replay_point(name: str, lat: float, lon: float, observed_at: str, band2: Band2Sampler) -> dict:
    when = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    elevation, bearing = solar_position(when, lat, lon)
    candidate = {
        "lat": lat, "lon": lon, "sunElevationDeg": elevation,
        "sunBearingDeg": bearing, "antiSolarBearingDeg": (bearing + 180) % 360,
    }
    dsrf_raw = sample(lat, lon, DEFAULT_PRODUCT, "auto", 3, CACHE / "goes", observed_at=observed_at)
    acmc_raw = sample(lat, lon, FALLBACK_PRODUCT, "auto", 3, CACHE / "goes", observed_at=observed_at)
    dsrf = dsrf_shadow(dsrf_raw, elevation)
    acmc = acmc_neighborhood(acmc_raw)
    band2_feature = band2.sample_candidate(candidate)
    metar = {
        "available": False, "supportScore": None,
        "unavailableReason": "historical METAR is not retained by the current-cache ingestion path",
    }
    probability, components = combine_shadow(band2_feature, acmc, metar, dsrf)
    return {
        "name": name, "lat": lat, "lon": lon,
        "sunElevationDeg": round(elevation, 2), "sunBearingDeg": round(bearing, 2),
        "directSunProbability": probability, "shadowCategory": shadow_category(probability),
        "probabilityStatus": "provisional-uncalibrated", "componentsUsed": components,
        "band2": band2_feature, "acmc": acmc, "dsrf": dsrf, "metar": metar,
    }


def main() -> int:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    scans = {scan["observedAt"]: scan for scan in replay["scans"]}
    samples = []
    try:
        for observed_at in TIMES:
            scan = scans[observed_at]
            seed = scan["strides"]["10"]["nearest"]["Baltimore"]
            band2 = Band2Sampler(observed_at, CACHE / "band2")
            try:
                points = [
                    replay_point("Baltimore observer reference", **replay["places"]["Baltimore"], observed_at=observed_at, band2=band2),
                    replay_point("Dundalk observer reference", **replay["places"]["Dundalk"], observed_at=observed_at, band2=band2),
                    replay_point("nearest production-stride seed", lat=seed["observerLat"], lon=seed["observerLon"], observed_at=observed_at, band2=band2),
                ]
            finally:
                band2.close()
            samples.append({"observedAt": observed_at, "points": points})
    finally:
        cleanup_shadow_cache(CACHE)

    payload = {
        "schemaVersion": 1, "methodVersion": METHOD_VERSION,
        "purpose": "Historical sunlight-v2 replay for the confirmed Baltimore/Dundalk bow.",
        "observationWindow": replay["observationWindow"],
        "mrmsReplayContentSha256": hashlib.sha256(REPLAY.read_bytes()).hexdigest(),
        "historicalMetarAvailable": False,
        "interpretationGuard": "Shadow probabilities are provisional and omit METAR; this is a feature replay, not an operational classification.",
        "samples": samples,
    }
    payload["contentSha256"] = canonical_hash(payload)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    summary = [{
        "observedAt": row["observedAt"],
        "points": [{
            "name": point["name"], "category": point["shadowCategory"],
            "probability": point["directSunProbability"],
            "band2MaxGapFraction": point["band2"]["maxGapFraction"],
            "acmcSupport": point["acmc"]["supportScore"],
            "dsrfUsable": point["dsrf"]["usable"],
        } for point in row["points"]],
    } for row in samples]
    print(json.dumps({"output": str(OUTPUT), "contentSha256": payload["contentSha256"], "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
