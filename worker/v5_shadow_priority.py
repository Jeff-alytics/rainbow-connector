"""Prospective V5 dual-cohort scoring and compact Review feed.

V5 never changes the operational detector or V4.2 acquisition.  It scores every
retained expanded seed and identifies the exact V4.2 representative geometries
as the nested overlap cohort.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from v4_shadow_review import attach_seeds

RULE_VERSION = "v5-dual-cohort-2026-08-v1"
MODEL_VERSION = "causal-spatial-phase-priority-v2"
SCHEMA_VERSION = "v5-shadow-prediction.v2"
EXPANDED_SEED_CONTRACT = "v5-expanded-camera-independent-seeds.v1"
GEOMETRY_IDENTITY_CONTRACT = "observer-rain-rounded-4dp.v1"
TRAILING_WINDOW_MINUTES = 30
EXPLORATION_FRACTION = 0.15
V4_MINIMUM_PERSISTENCE_SCANS = 3
V4_MINIMUM_CAUSAL_PEAK_RADAR = 95.0
V4_MINIMUM_CURRENT_RADAR = 70.0


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _seed_key(seed: dict) -> tuple:
    """Spatial identity only: observer/rain coordinates rounded to four decimals.

    Solar elevation and bow bearing are time-varying evidence on a location and
    deliberately do not create a second geometry identity.
    """
    return tuple(round(float(seed.get(name)), 4) if _finite(seed.get(name)) is not None else None
                 for name in ("lat", "lon", "rainLat", "rainLon"))


def _geometry_id(seed: dict) -> str:
    return _hash({"contract": GEOMETRY_IDENTITY_CONTRACT, "coordinates": _seed_key(seed)})[:20]


def _valid_expanded_seed(seed: dict) -> tuple[bool, list[str]]:
    reasons = []
    for name in ("lat", "lon", "rainLat", "rainLon", "radarScore"):
        if _finite(seed.get(name)) is None:
            reasons.append("missing_" + name)
    elevation = _finite(seed.get("sunElevationDeg"))
    if elevation is None:
        reasons.append("missing_sun_geometry")
    elif not -2 <= elevation <= 42:
        reasons.append("outside_physical_solar_window")
    return not reasons, reasons


def _trailing_edge(decay: float) -> float:
    if decay <= 0:
        return 0.0
    if decay < 5:
        return 0.25
    if decay < 10:
        return 0.75
    return 1.0


def _solar_feasibility(elevation: float | None) -> float:
    if elevation is None or elevation < -2 or elevation > 42:
        return 0.0
    if elevation < 0:
        return 0.25 * (elevation + 2) / 2
    if elevation < 5:
        return 0.25 + 0.75 * elevation / 5
    if elevation <= 25:
        return 1.0
    return max(0.05, 1.0 - 0.95 * (elevation - 25) / 17)


def _solar_lane(elevation: float | None) -> str:
    if elevation is None:
        return "unresolved"
    if 0 <= elevation <= 25:
        return "observed_core"
    if 25 < elevation <= 32:
        return "observed_extended"
    return "physical_audit"


def _exploration_selected(candidate_id: str, scan_time: str) -> tuple[bool, float]:
    digest = hashlib.sha256(f"{RULE_VERSION}|{candidate_id}|{scan_time[:10]}".encode()).hexdigest()
    value = int(digest[:16], 16) / float(16 ** 16)
    return value < EXPLORATION_FRACTION, value


def _features(record: dict, bounded_peak: float) -> dict:
    review = record["features"]["researchReview"]
    rain = record["features"].get("rain") or {}
    geometry = record["features"].get("geometry") or {}
    current = _finite(review.get("currentRadar")) or 0.0
    scans = max(1, int(review.get("persistenceScans") or 1))
    support = rain.get("spatialSupport") or {}
    adjacent = _finite(support.get("adjacentWetCells"))
    span = _finite(rain.get("antiSolarRainArcSpanDeg"))
    observer_rain = _finite(rain.get("observerRateMmHr"))
    local_support = None if adjacent is None else _clamp(adjacent / 8.0)
    arc_support = None if span is None else _clamp(span / 90.0)
    decay = max(0.0, bounded_peak - current)
    elevation = _finite(geometry.get("apparentSunElevationDeg"))
    if elevation is None:
        elevation = _finite(geometry.get("sunElevationDeg"))
    return {
        "persistence": _clamp(scans / 5),
        "persistenceScans": scans,
        "persistenceEvidenceStatus": "lineage_scan_exposure_proxy",
        "peakRadarBounded": bounded_peak,
        "peakRadarBoundedNormalized": _clamp(bounded_peak / 100),
        "currentRadar": current,
        "currentRadarPlausibility": _clamp(current / 100),
        "trailingEdgeDecay": round(decay, 3),
        "trailingEdgeBinned": _trailing_edge(decay),
        "localSupportFraction": local_support,
        "localSupportMethod": "mrms-3x3-adjacent-wet-fraction-v1",
        "antiSolarArcSpanDeg": span,
        "antiSolarArcSpanNormalized": arc_support,
        "spatialEvidenceStatus": "complete" if local_support is not None and arc_support is not None else "unresolved",
        "observerRainRateMmHr": observer_rain,
        "observerDryness": None if observer_rain is None else _clamp(1 - observer_rain / 2),
        "solarElevationDeg": elevation,
        "solarFeasibility": _solar_feasibility(elevation),
    }


def _score(features: dict, include_decay: bool) -> tuple[float, float, float]:
    local = 0.5 if features["localSupportFraction"] is None else features["localSupportFraction"]
    arc = 0.5 if features["antiSolarArcSpanNormalized"] is None else features["antiSolarArcSpanNormalized"]
    dryness = 0.5 if features["observerDryness"] is None else features["observerDryness"]
    support_weight = 0.20 if include_decay else 0.28
    evidence = (
        0.18 * features["persistence"]
        + 0.10 * features["peakRadarBoundedNormalized"]
        + 0.18 * features["currentRadarPlausibility"]
        + (0.08 * features["trailingEdgeBinned"] if include_decay else 0.0)
        + support_weight * local
        + 0.16 * arc
        + 0.10 * dryness
    )
    point = 100 * features["solarFeasibility"] * evidence
    missing = (support_weight if features["localSupportFraction"] is None else 0.0) + (
        0.16 if features["antiSolarArcSpanNormalized"] is None else 0.0) + (
        0.10 if features["observerDryness"] is None else 0.0)
    uncertainty = 100 * features["solarFeasibility"] * 0.5 * missing
    return round(point, 3), round(max(0.0, point - uncertainty), 3), round(min(100.0, point + uncertainty), 3)


def _v4_exclusions(scans: int, causal_peak: float, current: float, seed: dict, overlap: bool) -> list[str]:
    if overlap:
        return []
    reasons = []
    elevation = _finite(seed.get("sunElevationDeg"))
    observer_rain = _finite(seed.get("observerRainRateMmHr"))
    support = seed.get("spatialSupport") or {}
    adjacent = _finite(support.get("adjacentWetCells"))
    if elevation is not None and not 0 <= elevation <= 30:
        reasons.append("v4_solar_window")
    if observer_rain is not None and observer_rain >= 0.1:
        reasons.append("v4_observer_dry_gate")
    if support.get("isolatedPixel") is True or (adjacent is not None and adjacent < 1):
        reasons.append("v4_spatial_support_gate")
    if adjacent is None and support.get("connectedWetCellCount") is None:
        reasons.append("v4_spatial_support_unknown")
    if scans < V4_MINIMUM_PERSISTENCE_SCANS:
        reasons.append("v4_minimum_persistence")
    if causal_peak < V4_MINIMUM_CAUSAL_PEAK_RADAR:
        reasons.append("v4_minimum_peak_radar")
    if current < V4_MINIMUM_CURRENT_RADAR:
        reasons.append("v4_minimum_current_radar")
    return reasons or ["v4_representative_geometry_not_selected"]


def _record(seed: dict, item: dict, scan_time: str) -> dict:
    family_id = item["eventId"]
    seed_digest = _hash({"familyEventId": family_id, "scanTime": scan_time, "seed": _seed_key(seed)})[:24]
    return {
        "candidateId": "v5-expanded-" + seed_digest,
        "disposition": "v5_shadow_retained",
        "decisionStage": "v5_expanded_mechanical_scoring",
        "decisionReasons": ["retained_in_uncapped_v5_expanded_pool"],
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
            "geometry": {
                "sunElevationDeg": _finite(seed.get("sunElevationDeg")),
                "apparentSunElevationDeg": _finite(seed.get("sunElevationDeg")),
                "sunBearingDeg": _finite(seed.get("sunBearingDeg")),
                "antiSolarBearingDeg": _finite(seed.get("antiSolarBearingDeg")),
                "radarScore": _finite(seed.get("radarScore")),
            },
            "researchReview": {
                "source": "v5_shadow", "familyEventId": family_id, "componentId": item["componentId"],
                "persistenceScans": int(item.get("lineageScanCount") or 1),
                "currentRadar": _finite(seed.get("radarScore")) or 0.0,
                "reviewOnly": True, "publicClassificationChanged": False,
                "acquisitionEnabled": False,
            },
        },
    }


def score_v5_shadow_records(
    sidecar: dict,
    v4_records: list[dict],
    previous: dict | None,
    scan_time: str,
    storm_ledger: dict,
) -> dict:
    """Build and score the expanded cohort without changing V4.2 disposition."""
    prior_state = (previous or {}).get("v5ShadowState") or {}
    histories = dict(prior_state.get("radarHistories") or {})
    all_time_peaks = dict(prior_state.get("causalPeaks") or {})
    observed = _parse(scan_time)
    cutoff = observed - timedelta(minutes=TRAILING_WINDOW_MINUTES)
    objects = {item["componentId"]: item for item in storm_ledger.get("stormObjects") or []}
    if sidecar.get("v5ExpandedSeedContract") != EXPANDED_SEED_CONTRACT or not isinstance(
            sidecar.get("v5ExpandedSeeds"), list):
        raise ValueError("V5 expanded seed contract is absent; refusing capped/gated V4 seed fallback")
    expanded_sidecar = {**sidecar, "candidateSeeds": sidecar["v5ExpandedSeeds"]}
    seeds_by_component, unattached = attach_seeds(expanded_sidecar, list(objects.values()))
    v4_by_family = {}
    for record in v4_records:
        review = (record.get("features") or {}).get("researchReview") or {}
        v4_by_family[review.get("familyEventId")] = record

    records, invalid = [], 0
    attached_seed_ids = {id(seed) for seeds in seeds_by_component.values() for seed in seeds}
    for component_id, seeds in seeds_by_component.items():
        item = objects[component_id]
        for seed in seeds:
            valid, _ = _valid_expanded_seed(seed)
            if valid:
                records.append(_record(seed, item, scan_time))
            else:
                invalid += 1
    # Storm-object hysteresis requires a >=2 mm/hr core inside a >=0.5 mm/hr
    # envelope. A geometrically valid lighter shaft must not disappear merely
    # because it has no object lineage yet; retain it as a one-scan fallback
    # family and report that status in the audit.
    retained_unattached = 0
    for seed in expanded_sidecar["candidateSeeds"]:
        if id(seed) in attached_seed_ids:
            continue
        valid, _ = _valid_expanded_seed(seed)
        if not valid:
            invalid += 1
            continue
        identity = _hash({"rainLat": round(float(seed["rainLat"]), 3),
            "rainLon": round(float(seed["rainLon"]), 3), "unattached": True})[:20]
        item = {
            "componentId": f"v5-unattached-component-{scan_time.replace(':', '').replace('-', '')}-{identity}",
            "eventId": "v5-unattached-family-" + identity,
            "lineageScanCount": 1,
        }
        records.append(_record(seed, item, scan_time))
        retained_unattached += 1

    current_by_family = {}
    for record in records:
        review = record["features"]["researchReview"]
        family_id = review["familyEventId"]
        current_by_family[family_id] = max(current_by_family.get(family_id, 0.0), review["currentRadar"])
    bounded_by_family = {}
    for family_id, current in current_by_family.items():
        history = [item for item in histories.get(family_id, []) if _parse(item["scanTime"]) >= cutoff]
        history.append({"scanTime": scan_time, "currentRadar": current})
        history = sorted({item["scanTime"]: item for item in history}.values(), key=lambda item: item["scanTime"])
        histories[family_id] = history
        bounded_by_family[family_id] = max(float(item["currentRadar"]) for item in history)
        all_time_peaks[family_id] = max(float(all_time_peaks.get(family_id) or 0), current)

    rows = []
    overlap_count = 0
    for record in records:
        review = record["features"]["researchReview"]
        family_id = review["familyEventId"]
        v4_record = v4_by_family.get(family_id)
        v4_review = ((v4_record or {}).get("features") or {}).get("researchReview") or {}
        overlap = bool(v4_record and _seed_key({
            "lat": record["features"]["observer"].get("lat"), "lon": record["features"]["observer"].get("lon"),
            "rainLat": record["features"]["rain"].get("lat"), "rainLon": record["features"]["rain"].get("lon"),
        }) == _seed_key({
            "lat": ((v4_record.get("features") or {}).get("observer") or {}).get("lat"),
            "lon": ((v4_record.get("features") or {}).get("observer") or {}).get("lon"),
            "rainLat": ((v4_record.get("features") or {}).get("rain") or {}).get("lat"),
            "rainLon": ((v4_record.get("features") or {}).get("rain") or {}).get("lon"),
        }))
        overlap_count += int(overlap)
        features = _features(record, bounded_by_family[family_id])
        score, low, high = _score(features, True)
        no_decay_score, _, _ = _score(features, False)
        selected, draw = _exploration_selected(record["candidateId"], scan_time)
        seed = {
            **record["features"]["observer"],
            "rainLat": record["features"]["rain"].get("lat"), "rainLon": record["features"]["rain"].get("lon"),
            "observerRainRateMmHr": record["features"]["rain"].get("observerRateMmHr"),
            "sunElevationDeg": record["features"]["geometry"].get("sunElevationDeg"),
            "spatialSupport": record["features"]["rain"].get("spatialSupport"),
        }
        v4_prediction = v4_review.get("v4Prediction") or {}
        exclusions = _v4_exclusions(
            review["persistenceScans"], all_time_peaks[family_id], current_by_family[family_id], seed, overlap,
        )
        frozen = {
            "schemaVersion": SCHEMA_VERSION, "ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION,
            "scanTime": scan_time, "familyEventId": family_id, "componentId": review["componentId"],
            "candidateId": record["candidateId"], "cohortMembership": ["expanded", *(["overlap"] if overlap else [])],
            "v4ExclusionReasons": exclusions,
            "sourceV4PredictionSha256": v4_prediction.get("predictionSha256"),
            "sourceV4Classification": v4_prediction.get("classification"),
            "sourceSunlightState": v4_prediction.get("sunlightState") if overlap else None,
            "assessmentProcessingAt": v4_prediction.get("assessmentProcessingAt"),
            "trailingWindowMinutes": TRAILING_WINDOW_MINUTES,
            "features": features, "score": score, "scoreSensitivityLow": low, "scoreSensitivityHigh": high,
            "noDecayAblationScore": no_decay_score,
            "auditStrata": {
                "solar30To42": 30 < (features["solarElevationDeg"] or -99) <= 42,
                "observerWet": (features["observerRainRateMmHr"] or 0) >= 0.1,
                "resolvedIsolated": features["localSupportFraction"] == 0,
                "spatialUnknown": features["spatialEvidenceStatus"] == "unresolved",
            },
            "solarLane": _solar_lane(features["solarElevationDeg"]),
            "exploration": {"selected": selected, "assignmentProbability": EXPLORATION_FRACTION,
                "draw": round(draw, 8), "emissionEnabled": False},
            "acquisitionEnabled": False,
        }
        digest = _hash(frozen)
        prediction = {**frozen, "predictionId": "v5-shadow-" + digest[:24], "predictionSha256": digest}
        review["v5Prediction"] = prediction
        if overlap:
            v4_review["v5Prediction"] = prediction
        rows.append({"record": record, "prediction": prediction})

    lane_order = {"observed_core": 0, "observed_extended": 1, "physical_audit": 2, "unresolved": 3}
    ordered = sorted(rows, key=lambda item: (
        lane_order.get(item["prediction"]["solarLane"], 3),
        -item["prediction"]["score"], item["prediction"]["candidateId"],
    ))
    for rank, item in enumerate(ordered, 1):
        item["prediction"]["rankWithinScan"] = rank
        item["prediction"]["poolSize"] = len(ordered)
    no_decay_ordered = sorted(rows, key=lambda item: (
        -item["prediction"]["noDecayAblationScore"], item["prediction"]["candidateId"],
    ))
    for rank, item in enumerate(no_decay_ordered, 1):
        item["prediction"]["noDecayRankWithinScan"] = rank
    for item in ordered:
        item["prediction"]["predictionSha256"] = _hash({
            key: value for key, value in item["prediction"].items() if key != "predictionSha256"
        })
    predictions = [item["prediction"] for item in ordered]
    manifest = {"scanTime": scan_time, "ruleVersion": RULE_VERSION, "predictions": predictions}
    audit = {
        **(sidecar.get("upstreamSeedAudit") or {}),
        "attachedExpandedSeeds": len(records),
        "unattachedExpandedSeeds": unattached,
        "retainedUnattachedSeeds": retained_unattached,
        "invalidExpandedSeeds": invalid,
        "overlapGeometries": overlap_count,
        "expandedOnlyGeometries": len(records) - overlap_count,
        "cohortACountParity": {"v4Records": len(v4_records), "overlapGeometries": overlap_count,
            "passed": overlap_count == len(v4_records)},
    }
    return {
        "schemaVersion": "v5-candidate-scan.v1", "scanTime": scan_time,
        "ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION, "retained": len(records),
        "overlap": overlap_count, "expandedOnly": len(records) - overlap_count,
        "predictionManifestSha256": _hash(manifest), "predictions": predictions,
        "records": [item["record"] for item in ordered], "upstreamAudit": audit,
        "acquisitionEnabled": False,
        "state": {"ruleVersion": RULE_VERSION, "modelVersion": MODEL_VERSION,
            "radarHistories": histories, "causalPeaks": all_time_peaks},
    }


def compact_v5_feed(selection: dict) -> dict:
    """Return every V5 family in one compact scan-level projection.

    All geometries remain authoritative in S3 and every distinct geometry is
    included in its family's compact review projection. The top-level fields
    describe the best-ranked geometry so existing consumers stay simple.
    """
    by_family = {}
    records = {record["candidateId"]: record for record in selection.get("records") or []}
    for prediction in selection.get("predictions") or []:
        record = records.get(prediction.get("candidateId")) or {}
        features = record.get("features") or {}
        observer, rain, geometry = features.get("observer") or {}, features.get("rain") or {}, features.get("geometry") or {}
        row = {
            "predictionId": prediction["predictionId"], "predictionSha256": prediction["predictionSha256"],
            "candidateId": prediction["candidateId"], "familyEventId": prediction["familyEventId"],
            "detectedAt": prediction["scanTime"], "lat": observer.get("lat"), "lon": observer.get("lon"),
            "rainLat": rain.get("lat"), "rainLon": rain.get("lon"),
            "bowBearingDeg": geometry.get("antiSolarBearingDeg"),
            "sunElevationDeg": geometry.get("sunElevationDeg"),
            "score": prediction["score"], "rankWithinScan": prediction["rankWithinScan"],
            "poolSize": prediction["poolSize"], "noDecayAblationScore": prediction["noDecayAblationScore"],
            "noDecayRankWithinScan": prediction["noDecayRankWithinScan"],
            "cohortMembership": prediction["cohortMembership"],
            "v4ExclusionReasons": prediction["v4ExclusionReasons"],
            "auditStrata": prediction["auditStrata"], "exploration": prediction["exploration"],
            "solarLane": prediction["solarLane"],
            "acquisitionEnabled": False, "geometrySelections": 1,
        }
        geometry_row = {
            "geometryId": _geometry_id({
                "lat": observer.get("lat"), "lon": observer.get("lon"),
                "rainLat": rain.get("lat"), "rainLon": rain.get("lon"),
            }),
            "candidateId": prediction["candidateId"], "predictionId": prediction["predictionId"],
            "detectedAt": prediction["scanTime"], "lat": observer.get("lat"), "lon": observer.get("lon"),
            "rainLat": rain.get("lat"), "rainLon": rain.get("lon"),
            "bowBearingDeg": geometry.get("antiSolarBearingDeg"),
            "sunElevationDeg": geometry.get("sunElevationDeg"),
            "score": prediction["score"], "rankWithinScan": prediction["rankWithinScan"],
            "solarLane": prediction["solarLane"],
        }
        family_id = prediction["familyEventId"]
        state = by_family.setdefault(family_id, {
            "representative": row, "geometries": {}, "geometrySelections": 0, "solarLaneCounts": {},
        })
        state["geometrySelections"] += 1
        lane = prediction["solarLane"]
        state["solarLaneCounts"][lane] = state["solarLaneCounts"].get(lane, 0) + 1
        prior_geometry = state["geometries"].get(geometry_row["geometryId"])
        if prior_geometry is None or (geometry_row["rankWithinScan"], geometry_row["predictionId"]) < (
                prior_geometry["rankWithinScan"], prior_geometry["predictionId"]):
            state["geometries"][geometry_row["geometryId"]] = geometry_row
        if (row["rankWithinScan"], row["predictionId"]) < (
                state["representative"]["rankWithinScan"], state["representative"]["predictionId"]):
            state["representative"] = row
    items = []
    for family_id, state in by_family.items():
        row = state["representative"]
        geometries = sorted(state["geometries"].values(),
                            key=lambda item: (item["rankWithinScan"], item["geometryId"]))
        row.update({
            "geometries": geometries,
            "geometrySelections": state["geometrySelections"],
            "uniqueGeometryCount": len(geometries),
            "solarLaneCounts": dict(sorted(state["solarLaneCounts"].items())),
            "solarLaneMembership": sorted(state["solarLaneCounts"],
                key=lambda lane: ({"observed_core": 0, "observed_extended": 1,
                    "physical_audit": 2, "unresolved": 3}.get(lane, 4), lane)),
        })
        items.append(row)
    items.sort(key=lambda item: (item["rankWithinScan"], item["familyEventId"]))
    unique_geometries = sum(item["uniqueGeometryCount"] for item in items)
    return {
        "schemaVersion": "v5-candidate-scan.v1", "scanTime": selection["scanTime"],
        "geometryIdentityContract": GEOMETRY_IDENTITY_CONTRACT,
        "ruleVersion": selection["ruleVersion"], "modelVersion": selection["modelVersion"],
        "predictionManifestSha256": selection["predictionManifestSha256"],
        "retained": selection["retained"], "overlap": selection["overlap"],
        "expandedOnly": selection["expandedOnly"], "acquisitionEnabled": False,
        "upstreamAudit": selection.get("upstreamAudit") or {}, "items": items,
        "retainedSelections": selection["retained"], "retainedGeometries": unique_geometries,
        "retainedFamilies": len(items),
    }
