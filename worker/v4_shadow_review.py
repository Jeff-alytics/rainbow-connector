"""Frozen-mechanics V4 selector for the private, post-publication review lane."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict

from opportunity_ledger import build_storm_object_ledger
from research_review import camera_matches

RULE_VERSION = "v4-shadow-review-2026-08-v2"
MODEL_VERSION = "causal-bounded-persistence-radar-v2"
SOURCE = "v4_shadow"
MECHANICAL_FREEZE_CONTENT_SHA256 = "ebf849586c6b529e0c31231f4a9c8c5009152042c0e9bb7ed6da726f878c56e5"
PERSISTENCE_SATURATION_SCANS = 5
MINIMUM_PERSISTENCE_SCANS = 3
MINIMUM_CAUSAL_PEAK_RADAR = 95.0
MINIMUM_CURRENT_RADAR = 70.0


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def valid_seed(seed: dict) -> tuple[bool, list[str]]:
    reasons = []
    elevation = _finite(seed.get("sunElevationDeg"))
    required = {
        "observer_location": _finite(seed.get("lat")) is not None and _finite(seed.get("lon")) is not None,
        "rain_location": _finite(seed.get("rainLat")) is not None and _finite(seed.get("rainLon")) is not None,
        "radar_score": _finite(seed.get("radarScore")) is not None,
        "sun_geometry": elevation is not None and 0 <= elevation <= 30,
    }
    reasons.extend(name for name, passed in required.items() if not passed)
    observer_rain = _finite(seed.get("observerRainRateMmHr"))
    if observer_rain is not None and observer_rain >= 0.1:
        reasons.append("observer_not_dry")
    support = seed.get("spatialSupport") or {}
    neighbors = _finite(support.get("adjacentWetCells"))
    if support.get("isolatedPixel") is True or (neighbors is not None and neighbors < 1):
        reasons.append("radar_spatial_support")
    if neighbors is None and support.get("connectedWetCellCount") is None:
        reasons.append("radar_spatial_support_unknown")
    return not reasons, reasons


def _object_index(objects: list[dict]) -> dict:
    rows = defaultdict(list)
    for item in objects:
        for row, first, last, *_ in item.get("runs") or []:
            rows[int(row)].append((int(first), int(last), item["componentId"]))
    result = {}
    for row, intervals in rows.items():
        intervals.sort()
        result[row] = ([item[0] for item in intervals], intervals)
    return result


def attach_seeds(sidecar: dict, objects: list[dict]) -> tuple[dict[str, list[dict]], int]:
    grid = sidecar["grid"]
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    index, attached, unattached = _object_index(objects), defaultdict(list), 0
    for seed in sidecar.get("candidateSeeds") or []:
        if _finite(seed.get("rainLat")) is None or _finite(seed.get("rainLon")) is None:
            unattached += 1
            continue
        row = round((float(seed["rainLat"]) - lat0) / dlat)
        column = round((float(seed["rainLon"]) - lon0) / dlon)
        starts, intervals = index.get(row, ([], []))
        position = bisect.bisect_right(starts, column) - 1
        if position < 0 or column > intervals[position][1]:
            unattached += 1
            continue
        attached[intervals[position][2]].append(seed)
    return attached, unattached


def _candidate_id(event_id: str, scan_time: str) -> str:
    return "v4-shadow-" + hashlib.sha256(f"{event_id}|{scan_time}".encode()).hexdigest()[:24]


def _record(candidate: dict, prediction: dict, camera_catalog: list[dict]) -> dict:
    seed = candidate["representativeSeed"]
    geometry = {
        "sunElevationDeg": _finite(seed.get("sunElevationDeg")),
        "apparentSunElevationDeg": _finite(seed.get("sunElevationDeg")),
        "sunBearingDeg": _finite(seed.get("sunBearingDeg")),
        "antiSolarBearingDeg": _finite(seed.get("antiSolarBearingDeg")),
        "radarScore": _finite(seed.get("radarScore")),
    }
    record = {
        "candidateId": _candidate_id(candidate["eventId"], prediction["scanTime"]),
        "disposition": "selected_research_possible",
        "decisionStage": "v4_shadow_review_selection",
        "decisionReasons": ["selected_by_frozen_v4_shadow_rank"],
        "features": {
            "observer": {"lat": _finite(seed.get("lat")), "lon": _finite(seed.get("lon"))},
            "rain": {
                "lat": _finite(seed.get("rainLat")), "lon": _finite(seed.get("rainLon")),
                "distanceKm": _finite(seed.get("rainDistanceKm")), "rateMmHr": _finite(seed.get("rainRateMmHr")),
                "observerRateMmHr": _finite(seed.get("observerRainRateMmHr")),
                "spatialSupport": seed.get("spatialSupport"),
                "antiSolarRainArcSpanDeg": seed.get("antiSolarRainArcSpanDeg"),
                "antiSolarRainSpanByTier": seed.get("antiSolarRainSpanByTier"),
            },
            "geometry": geometry,
            "thresholdSnapshot": {
                "minimumObjectPersistenceScans": MINIMUM_PERSISTENCE_SCANS,
                "minimumCausalPeakRadar": MINIMUM_CAUSAL_PEAK_RADAR,
                "minimumCurrentRadar": MINIMUM_CURRENT_RADAR,
                "eligibleCandidateCap": None,
            },
        },
    }
    matches = camera_matches(record, camera_catalog or [])
    record["features"]["researchReview"] = {
        "source": SOURCE, "ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION,
        "lane": prediction["lane"], "rankWithinScan": prediction["rankWithinScan"],
        "poolSize": prediction["poolSize"], "score": candidate["score"],
        "causalPeakRadar": candidate["causalPeakRadar"], "currentRadar": candidate["currentRadar"],
        "persistenceScans": candidate["objectPersistenceScans"],
        "ledgerEventId": candidate["eventId"], "familyEventId": candidate["eventId"],
        "componentId": candidate["componentId"], "cameraMatches": matches,
        "hasMatchedCamera": bool(matches), "reviewOnly": True, "publicClassificationChanged": False,
        "selectionReason": "all mechanically eligible families retained for causal sunlight evaluation",
        "currentDetectorDisposition": "private_post_publication_shadow_candidate",
        "v4Prediction": prediction,
    }
    return record


def select_v4_shadow_records(sidecar: dict, previous: dict | None, camera_catalog: list[dict] | None) -> dict:
    prior_state = (previous or {}).get("v4ShadowState") or {}
    storm_ledger = build_storm_object_ledger(sidecar, prior_state.get("stormLedger"))
    objects = {item["componentId"]: item for item in storm_ledger.get("stormObjects") or []}
    seeds_by_component, unattached = attach_seeds(sidecar, list(objects.values()))
    prior_families = prior_state.get("families") or {}
    next_families, candidates = dict(prior_families), []
    for component_id, seeds in seeds_by_component.items():
        item = objects[component_id]
        ancestors = sorted(set(item.get("ancestorEventIds") or [item["eventId"]]))
        valid = [seed for seed in seeds if valid_seed(seed)[0]]
        if not valid:
            continue
        current = max(float(seed["radarScore"]) for seed in valid)
        inherited_peak = max((_finite((prior_families.get(key) or {}).get("peakRadar")) or 0 for key in ancestors), default=0)
        peak = max(inherited_peak, current)
        previous_current = max((_finite((prior_families.get(key) or {}).get("currentRadar")) or current for key in ancestors), default=current)
        scans = int(item.get("lineageScanCount") or 1)
        persistence = min(scans, PERSISTENCE_SATURATION_SCANS) / PERSISTENCE_SATURATION_SCANS
        trend = max(-1.0, min(1.0, (current - previous_current) / 20))
        best_seed, best_score = None, -1.0
        for seed in valid:
            score = max(0.0, min(100.0, 100 * (0.45 * peak / 100 + 0.35 * persistence
                + 0.15 * float(seed["radarScore"]) / 100 + 0.05 * (trend + 1) / 2)))
            if score > best_score:
                best_seed, best_score = seed, score
        family_state = {"peakRadar": round(peak, 3), "currentRadar": round(current, 3), "scanTime": sidecar["observedAt"]}
        for ancestor in ancestors:
            next_families[ancestor] = family_state
        candidates.append({
            "componentId": component_id, "eventId": item["eventId"], "ancestorEventIds": ancestors,
            "objectPersistenceScans": scans, "causalPeakRadar": round(peak, 3),
            "currentRadar": round(current, 3), "score": round(best_score, 3),
            "eligible": scans >= MINIMUM_PERSISTENCE_SCANS and peak >= MINIMUM_CAUSAL_PEAK_RADAR and current >= MINIMUM_CURRENT_RADAR,
            "representativeSeed": best_seed, "validSeedCount": len(valid), "attachedSeedCount": len(seeds),
        })
    # Frozen split rule: reduce siblings to one family representative before eligibility filtering.
    by_event = {}
    for candidate in candidates:
        old = by_event.get(candidate["eventId"])
        if old is None or (candidate["score"], candidate["causalPeakRadar"], candidate["componentId"]) > (old["score"], old["causalPeakRadar"], old["componentId"]):
            by_event[candidate["eventId"]] = candidate
    eligible = sorted((item for item in by_event.values() if item["eligible"]), key=lambda item: (
        -item["score"], -item["causalPeakRadar"], -item["objectPersistenceScans"], item["eventId"]))
    prediction_rows, records = [], []
    pool_size = len(eligible)
    for rank, candidate in enumerate(eligible, 1):
        lane = "evaluation_pending"
        frozen = {
            "schemaVersion": "v4-shadow-prediction.v2", "scanTime": sidecar["observedAt"],
            "ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION,
            "mechanicalFreezeContentSha256": MECHANICAL_FREEZE_CONTENT_SHA256,
            "familyEventId": candidate["eventId"], "componentId": candidate["componentId"],
            "score": candidate["score"], "causalPeakRadar": candidate["causalPeakRadar"],
            "currentRadar": candidate["currentRadar"], "persistenceScans": candidate["objectPersistenceScans"],
            "rankWithinScan": rank, "poolSize": pool_size, "lane": lane,
            "sunlightDecisionPolicy": "causal-sunlight-state-v2",
        }
        digest = _canonical_hash(frozen)
        prediction = {**frozen, "predictionId": f"v4-shadow-{sidecar['observedAt'].replace('-', '').replace(':', '')}-{rank}-{digest[:12]}", "predictionSha256": digest}
        prediction_rows.append(prediction)
        records.append(_record(candidate, prediction, camera_catalog or []))
    manifest_payload = {"scanTime": sidecar["observedAt"], "ruleVersion": RULE_VERSION, "predictions": prediction_rows}
    manifest_hash = _canonical_hash(manifest_payload)
    state = {
        "ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION,
        "stormLedger": storm_ledger, "families": next_families,
        "decisions": dict(prior_state.get("decisions") or {}),
        "projectionBacklog": list(prior_state.get("projectionBacklog") or []),
    }
    return {
        "ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION, "selected": len(records),
        "retainedEligible": len(records),
        "eligibleFamilies": pool_size, "unattachedSeeds": unattached,
        "predictionManifestSha256": manifest_hash, "predictions": prediction_rows,
        "selectedCandidateIds": [item["candidateId"] for item in records], "records": records, "state": state,
    }


def finalize_v42_records(records: list[dict], state: dict, assessment_processing_at: str) -> dict:
    """Freeze sunlight-aware decisions and select state-change-only projections."""
    prior_decisions = state.get("decisions") or {}
    next_decisions = dict(prior_decisions)
    dispatch_records = []
    counts = defaultdict(int)
    decision_day = assessment_processing_at[:10]
    for record in records:
        review = record["features"]["researchReview"]
        sunlight = record["features"].get("sunlightV2") or {}
        sunlight_state = sunlight.get("sunlightState")
        if sunlight_state == "sunlit_supported":
            classification, lane = "GO_SUNLIT_SUPPORTED", "go"
        elif sunlight_state == "sunlight_plausible":
            classification, lane = "POSSIBLE_PLAUSIBLE", "possible"
        elif sunlight_state == "overcast_supported":
            classification, lane = "WITHHELD_OVERCAST", "withheld"
        elif sunlight.get("available") is False or sunlight.get("error"):
            classification, lane = "POSSIBLE_UNAVAILABLE", "possible"
        else:
            classification, lane = "POSSIBLE_UNRESOLVED", "possible"
        family_id = review["familyEventId"]
        previous = prior_decisions.get(family_id) or {}
        dispatch = lane != "withheld" and (
            previous.get("classification") != classification
            or previous.get("lastDispatchedDay") != decision_day
        )
        frozen = {
            **review["v4Prediction"],
            "mechanicalPredictionSha256": review["v4Prediction"]["predictionSha256"],
            "lane": lane,
            "classification": classification,
            "assessmentProcessingAt": assessment_processing_at,
            "sunlightMethodVersion": sunlight.get("methodVersion"),
            "sunlightState": sunlight_state or "unavailable",
            "sunlightStateReasons": sunlight.get("sunlightStateReasons") or [],
        }
        frozen["predictionSha256"] = _canonical_hash({
            key: value for key, value in frozen.items() if key != "predictionSha256"
        })
        review.update({
            "lane": lane, "classification": classification,
            "selectionReason": "causal sunlight supported" if lane == "go"
                else "causal sunlight plausible or unresolved" if lane == "possible"
                else "affirmative causal overcast evidence",
            "v4Prediction": frozen,
        })
        record["disposition"] = "selected_research_possible" if lane != "withheld" else "withheld_research_overcast"
        record["decisionStage"] = "v4_shadow_sunlight_decision"
        record["decisionReasons"] = [classification.lower()]
        counts[classification] += 1
        next_decisions[family_id] = {
            "classification": classification,
            "scanTime": frozen["scanTime"],
            "lastDispatchedDay": previous.get("lastDispatchedDay"),
        }
        if dispatch:
            dispatch_records.append(record)
    state["decisions"] = next_decisions
    return {"records": records, "dispatchRecords": dispatch_records, "classificationCounts": dict(counts)}


def mark_v42_dispatched(state: dict, records: list[dict], dispatched_at: str) -> None:
    """Record a successful Review projection without affecting retained records."""
    day = dispatched_at[:10]
    decisions = state.setdefault("decisions", {})
    for record in records:
        review = (record.get("features") or {}).get("researchReview") or {}
        family_id = review.get("familyEventId")
        if family_id and family_id in decisions:
            decisions[family_id]["lastDispatchedDay"] = day
