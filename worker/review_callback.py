"""Compact, non-operational sunlight-v2 assessments for the Review Workbench."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import requests

from decision_log import RULE_VERSION
from sunlight_v2 import METHOD_VERSION

CALLBACK_SCHEMA_VERSION = "review-assessment.v1"
CALLBACK_MAX_BYTES = 128 * 1024
TRANSMITTED_DISPOSITIONS = {
    "selected_go", "selected_possible", "selected_research_possible", "research_selected_possible",
}


def finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def compact_source(source: dict | None) -> dict | None:
    if not source:
        return None
    return {
        key: source.get(key) for key in (
            "available", "observedAt", "createdAt", "sourceKey", "causalEligible",
            "unavailableReason", "methodVersion", "supportScore", "clearnessRatio",
        ) if source.get(key) is not None
    }


def compact_assessment(record: dict, envelope: dict) -> dict | None:
    if record.get("disposition") not in TRANSMITTED_DISPOSITIONS:
        return None
    features = record.get("features") or {}
    sunlight = features.get("sunlightV2") or {}
    if sunlight.get("methodVersion") != METHOD_VERSION:
        return None
    band2 = sunlight.get("band2") or {}
    metar = sunlight.get("metar") or {}
    radar = envelope.get("radar") or {}
    return {
        "candidateId": record.get("candidateId"),
        "disposition": record.get("disposition"),
        "decisionStage": record.get("decisionStage"),
        "decisionReasons": record.get("decisionReasons") or [],
        "observer": features.get("observer") or {},
        "rain": features.get("rain") or {},
        "geometry": features.get("geometry") or {},
        "thresholdSnapshot": features.get("thresholdSnapshot") or {},
        "researchReview": features.get("researchReview"),
        "radarObservedAt": radar.get("observedAt") or sunlight.get("radarObservedAt"),
        "assessmentProcessingAt": sunlight.get("assessmentProcessingAt"),
        "enrichmentLatencySeconds": finite(sunlight.get("enrichmentLatencySeconds")),
        "sunlightState": sunlight.get("sunlightState"),
        "sunlightStateReasons": sunlight.get("sunlightStateReasons") or [],
        "directSunProbability": finite(sunlight.get("directSunProbability")),
        "probabilityStatus": sunlight.get("probabilityStatus"),
        "v1SunlightCategory": sunlight.get("v1SunlightCategory"),
        "disagreesWithV1": sunlight.get("disagreesWithV1"),
        "sources": {
            "band2": {
                "methodVersion": band2.get("normalizationVersion"),
                "causalGapFraction": finite(band2.get("causalGapFraction")),
                "framesUsed": band2.get("temporalFramesUsed"),
                "corroboratingFrames": band2.get("corroboratingFrames"),
                "frames": [{
                    key: frame.get(key) for key in ("s3Key", "observedAt", "createdAt", "causalEligible")
                    if frame.get(key) is not None
                } for frame in (band2.get("frames") or [])],
            },
            "acmc": compact_source(sunlight.get("acmc")),
            "dsrf": compact_source(sunlight.get("dsrf")),
            "metar": {
                "methodVersion": metar.get("methodVersion"),
                "availableBy": metar.get("availableBy"),
                "supportScore": finite(metar.get("supportScore")),
                "stations": [{
                    key: station.get(key) for key in ("stationId", "observedAt", "distanceKm", "causalEligible")
                    if station.get(key) is not None
                } for station in (metar.get("stations") or [])],
            },
        },
        "rainFootprint": {
            "id": radar.get("rainFootprintId"),
            "contentSha256": radar.get("rainFootprintContentSha256"),
        },
    }


def idempotency_key(assessment: dict, detector_rule_version: str) -> str:
    seed = "|".join((
        str(assessment.get("candidateId") or ""), str(assessment.get("radarObservedAt") or ""),
        detector_rule_version, METHOD_VERSION,
    ))
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def build_payload(envelope: dict) -> dict:
    detector_rule_version = envelope.get("detectorRuleVersion") or RULE_VERSION
    assessments = [item for record in (envelope.get("records") or []) if (item := compact_assessment(record, envelope))]
    for assessment in assessments:
        assessment["idempotencyKey"] = idempotency_key(assessment, detector_rule_version)
    return {
        "schemaVersion": CALLBACK_SCHEMA_VERSION,
        "detectorRuleVersion": detector_rule_version,
        "sunlightMethodVersion": METHOD_VERSION,
        "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "assessments": assessments,
    }


def payload_batches(payload: dict, maximum_bytes: int = CALLBACK_MAX_BYTES, maximum_items: int = 32) -> list[tuple[dict, bytes]]:
    header = {key: value for key, value in payload.items() if key != "assessments"}
    batches = []
    current = []
    for assessment in payload.get("assessments") or []:
        if len(current) >= maximum_items:
            completed = {**header, "assessments": current}
            batches.append((completed, json.dumps(completed, separators=(",", ":")).encode("utf-8")))
            current = []
        candidate = {**header, "assessments": [*current, assessment]}
        body = json.dumps(candidate, separators=(",", ":")).encode("utf-8")
        if len(body) <= maximum_bytes:
            current.append(assessment)
            continue
        if not current:
            raise ValueError("one review assessment exceeds the callback size limit")
        completed = {**header, "assessments": current}
        batches.append((completed, json.dumps(completed, separators=(",", ":")).encode("utf-8")))
        current = [assessment]
    if current:
        completed = {**header, "assessments": current}
        body = json.dumps(completed, separators=(",", ":")).encode("utf-8")
        if len(body) > maximum_bytes:
            raise ValueError("one review assessment exceeds the callback size limit")
        batches.append((completed, body))
    return batches


def callback_secret(ssm_client=None) -> str:
    direct = str(os.environ.get("REVIEW_ENRICH_SECRET") or "").strip()
    if direct:
        return direct
    parameter_name = str(os.environ.get("REVIEW_ENRICH_SECRET_PARAMETER") or "").strip()
    if not parameter_name:
        return ""
    if ssm_client is None:
        import boto3
        ssm_client = boto3.client("ssm")
    return str(ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)["Parameter"]["Value"]).strip()


def push_review_assessments(envelope: dict, session=None, secret: str | None = None) -> dict:
    url = str(os.environ.get("RAINBOW_REVIEW_ENRICH_URL") or "").strip()
    payload = build_payload(envelope)
    if not payload["assessments"]:
        return {"ok": True, "skipped": True, "reason": "no selected assessments", "assessments": 0}
    # A missing URL is the operational kill switch. Do not even fetch the
    # signing secret while the research-to-Review lane is paused.
    if not url:
        return {"ok": True, "skipped": True, "reason": "review callback not configured", "assessments": len(payload["assessments"])}
    secret = secret if secret is not None else callback_secret()
    if not secret:
        return {"ok": True, "skipped": True, "reason": "review callback not configured", "assessments": len(payload["assessments"])}
    session = session or requests.Session()
    results = []
    total_bytes = 0
    for _, body in payload_batches(payload):
        response = session.post(
            url, data=body,
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "User-Agent": "RainbowConnector/1.0"},
            timeout=30,
        )
        try:
            result = response.json()
        except Exception:
            result = {"body": response.text[:300]}
        if not response.ok:
            raise RuntimeError(f"Review assessment callback failed ({response.status_code}): {result}")
        results.append(result)
        total_bytes += len(body)
    aggregate = {
        name: sum(int(item.get(name) or 0) for item in results)
        for name in ("received", "attached", "duplicates", "unmatched")
    }
    return {"ok": True, "assessments": len(payload["assessments"]), "batches": len(results), "bytes": total_bytes, "response": aggregate}


def safe_push_review_assessments(envelope: dict) -> dict:
    try:
        return push_review_assessments(envelope)
    except Exception as error:
        print(f"[review-callback] push failed: {str(error)[:300]}")
        return {"ok": False, "operationalImpact": False, "error": str(error)[:300]}
