"""Select a tiny, camera-gated Opportunity Ledger lane for private review."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from detector_core import solar_position
from opportunity_ledger import _swath_cell, apparent_solar_elevation
from rain_footprint import RATE_TIERS_MM_HR, candidate_rain_span
from research_review import camera_matches

RULE_VERSION = "opportunity-ledger-review-2026-07-v1"
MAX_PER_SCAN = 2
MAX_CAMERA_DISTANCE_KM = 40.0


def _arrays(sidecar: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    grid = sidecar["grid"]
    latitudes = float(grid["latitudeStart"]) + np.arange(int(grid["latitudeCount"])) * float(grid["latitudeStepDeg"])
    longitudes = float(grid["longitudeStart"]) + np.arange(int(grid["longitudeCount"])) * float(grid["longitudeStepDeg"])
    rates = np.zeros((latitudes.size, longitudes.size), dtype=np.float32)
    for row, first, last, tier in sidecar.get("runs") or []:
        rates[int(row), int(first):int(last) + 1] = RATE_TIERS_MM_HR[int(tier) - 1]
    return latitudes, longitudes, rates


def _nearest_component_rain(component: dict, lat: float, lon: float, sidecar: dict) -> dict:
    grid = sidecar["grid"]
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    target_column = round((lon - lon0) / dlon)
    best = None
    for row, first, last, tier in component.get("runs") or []:
        column = max(int(first), min(int(last), target_column))
        rain_lat, rain_lon = lat0 + int(row) * dlat, lon0 + column * dlon
        north = (rain_lat - lat) * 111.0
        east = (rain_lon - lon) * 111.0 * math.cos(math.radians(lat))
        distance = math.hypot(north, east)
        if best is None or distance < best[0]:
            best = (distance, rain_lat, rain_lon, int(tier))
    if best is None:
        return {}
    distance, rain_lat, rain_lon, tier = best
    return {"lat": round(rain_lat, 4), "lon": round(rain_lon, 4),
            "distanceKm": round(distance, 1), "rateMmHr": RATE_TIERS_MM_HR[tier - 1], "rateTier": tier}


def _candidate_id(event_id: str, lat: float, lon: float, scan_time: str) -> str:
    seed = f"{event_id}|{lat:.4f}|{lon:.4f}|{scan_time}"
    return "ledger-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _base_record(opportunity: dict, representative: dict, scan_time: str) -> dict:
    lat, lon = float(representative["lat"]), float(representative["lon"])
    when = datetime.fromisoformat(scan_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    geometric, sun_bearing = solar_position(when, lat, lon)
    apparent = apparent_solar_elevation(geometric)
    return {
        "candidateId": _candidate_id(opportunity["eventId"], lat, lon, scan_time),
        "disposition": "selected_research_possible",
        "decisionStage": "opportunity_ledger_review_selection",
        "decisionReasons": ["selected_opportunity_ledger_camera_gated_review_v1"],
        "features": {
            "observer": {"lat": lat, "lon": lon}, "rain": {},
            "geometry": {"sunElevationDeg": round(geometric, 2), "apparentSunElevationDeg": round(apparent, 2),
                         "sunBearingDeg": round(sun_bearing, 2), "antiSolarBearingDeg": round((sun_bearing + 180) % 360, 2),
                         "radarScore": None},
            "thresholdSnapshot": {"minimumPersistenceScans": 2, "maximumCameraDistanceKm": MAX_CAMERA_DISTANCE_KM,
                                  "maximumCandidatesPerScan": MAX_PER_SCAN},
        },
    }


def select_ledger_review_records(ledger: dict, previous: dict | None, sidecar: dict,
                                 camera_catalog: list[dict] | None, maximum: int = MAX_PER_SCAN) -> dict:
    previous_events = {item.get("eventId") for item in (previous or {}).get("opportunities") or []}
    components = {item["componentId"]: item for item in ledger.get("rainEvents") or []}
    persistent = [item for item in ledger.get("opportunities") or [] if item.get("eventId") in previous_events]
    # Join from the 960 fixed FAA sites into a row-indexed swath instead of
    # multiplying every representative point by every camera. A one-cell
    # margin matches the ledger's documented 2–3 km placement tolerance.
    intervals = {}
    for opportunity in persistent:
        for row, first, last in (opportunity.get("observerSwath") or {}).get("runs") or []:
            intervals.setdefault(int(row), []).append((int(first), int(last), opportunity))
    candidates = []
    for site in camera_catalog or []:
        row, column = _swath_cell(float(site["lat"]), float(site["lon"]))
        nearby = {}
        for candidate_row in (row - 1, row, row + 1):
            for first, last, opportunity in intervals.get(candidate_row, []):
                if first - 1 <= column <= last + 1:
                    nearby[opportunity["opportunityId"]] = opportunity
        for opportunity in nearby.values():
            observer = {"lat": float(site["lat"]), "lon": float(site["lon"]), "derivedFromSwath": True}
            record = _base_record(opportunity, observer, ledger["scanTime"])
            matches = camera_matches(record, [site], maximum_distance_km=MAX_CAMERA_DISTANCE_KM)
            if matches:
                candidates.append((record, opportunity, matches))
    candidates.sort(key=lambda item: (item[2][0]["distanceKm"], -item[2][0]["visibleBowFraction"],
                                      -int((item[1].get("observerSwath") or {}).get("cellCount") or 0)))
    selected, used_cameras = [], set()
    for record, opportunity, matches in candidates:
        primary_key = (str(matches[0].get("siteId")), str(matches[0].get("cameraId")))
        if primary_key in used_cameras:
            continue
        used_cameras.add(primary_key)
        selected.append((record, opportunity, matches))
        if len(selected) >= max(0, min(int(maximum), MAX_PER_SCAN)):
            break

    latitudes = longitudes = rates = None
    if selected:
        latitudes, longitudes, rates = _arrays(sidecar)
    for rank, (record, opportunity, matches) in enumerate(selected, 1):
        observer = record["features"]["observer"]
        rain = _nearest_component_rain(components[opportunity["rainComponentId"]], observer["lat"], observer["lon"], sidecar)
        span = candidate_rain_span({**observer, "antiSolarBearingDeg": record["features"]["geometry"]["antiSolarBearingDeg"]},
                                   latitudes, longitudes, rates)
        nearest_row = int(np.abs(latitudes - observer["lat"]).argmin())
        nearest_column = int(np.abs(longitudes - observer["lon"]).argmin())
        rain.update({"observerRateMmHr": float(rates[nearest_row, nearest_column]),
                     "antiSolarRainSpanByTier": span, "antiSolarRainArcSpanDeg": span["any"]["arcSpanDeg"]})
        record["features"]["rain"] = rain
        record["features"]["researchReview"] = {
            "ruleVersion": RULE_VERSION, "source": "opportunity_ledger", "rankWithinScan": rank,
            "reviewOnly": True, "publicClassificationChanged": False,
            "selectionReason": "camera-gated persistent observer swath",
            "currentDetectorDisposition": "not_generated_as_detector_candidate",
            "ledgerEventId": opportunity["eventId"], "opportunityId": opportunity["opportunityId"],
            "ledgerMethodVersion": ledger.get("methodVersion"), "persistenceScans": 2, "cameraMatches": matches,
        }
    return {"ruleVersion": RULE_VERSION, "eligibleCameraMatches": len(candidates), "selected": len(selected),
            "selectedCandidateIds": [item[0]["candidateId"] for item in selected],
            "maximumPerScan": MAX_PER_SCAN, "records": [item[0] for item in selected]}


def load_faa_catalog() -> list[dict]:
    paths = [Path(__file__).resolve().parents[1] / "faa-sites-compact.json",
             Path(__file__).with_name("assets") / "faa-sites-compact.json"]
    path = next((item for item in paths if item.exists()), None)
    return json.loads(path.read_text(encoding="utf-8")) if path else []
