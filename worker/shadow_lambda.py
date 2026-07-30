"""Separate AWS Lambda entry point for non-operational sunlight-v2 enrichment."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from decision_store import SCHEMA_VERSION, disagreement_metrics, encoded
from ledger_review import load_faa_catalog
from research_review import select_research_candidates
from review_callback import safe_push_review_assessments
from sunlight_v2 import METHOD_VERSION, enrich_sunlight_v2


def already_enriched(envelope: dict) -> bool:
    shadow = envelope.get("shadowV2") or {}
    return shadow.get("methodVersion") == METHOD_VERSION and shadow.get("status") in {
        "complete", "complete_with_errors",
    }


def candidate_from_record(record: dict) -> dict | None:
    features = record.get("features") or {}
    observer, rain, geometry = features.get("observer") or {}, features.get("rain") or {}, features.get("geometry") or {}
    required = (observer.get("lat"), observer.get("lon"), rain.get("lat"), rain.get("lon"), geometry.get("sunElevationDeg"), geometry.get("sunBearingDeg"), geometry.get("antiSolarBearingDeg"))
    if any(value is None for value in required):
        return None
    return {
        "lat": observer["lat"], "lon": observer["lon"], "rainLat": rain["lat"], "rainLon": rain["lon"],
        "rainDistanceKm": rain.get("distanceKm"), "rainRateMmHr": rain.get("rateMmHr"),
        "observerRainRateMmHr": rain.get("observerRateMmHr"), "sunElevationDeg": geometry["sunElevationDeg"],
        "sunBearingDeg": geometry["sunBearingDeg"], "antiSolarBearingDeg": geometry["antiSolarBearingDeg"],
        "radarScore": geometry.get("radarScore"),
    }


def handler(event, context):
    bucket = str((event or {}).get("bucket") or os.environ.get("RAINBOW_RESEARCH_BUCKET") or "").strip()
    key = str((event or {}).get("key") or "").strip()
    if not bucket or not key.startswith("decision-log/rolling/"):
        raise ValueError("A rolling decision-log bucket and key are required")
    import boto3
    s3 = boto3.client("s3")
    response = s3.get_object(Bucket=bucket, Key=key)
    envelope = json.loads(gzip.decompress(response["Body"].read()))
    if already_enriched(envelope):
        return {
            "ok": True, "skipped": True, "reason": "shadow record already enriched",
            "methodVersion": METHOD_VERSION, "bucket": bucket, "key": key,
        }
    records = envelope.get("records") or []
    candidates = [candidate for record in records if (candidate := candidate_from_record(record)) is not None]
    radar_observed_at = (envelope.get("radar") or {}).get("observedAt") or envelope.get("generatedAt")
    assessment_processing_at = envelope.get("generatedAt")
    result = enrich_sunlight_v2(
        records, candidates, radar_observed_at,
        Path(os.environ.get("RAINBOW_CACHE_DIR", "/tmp/rainbow-shadow")),
        assessment_processing_at=assessment_processing_at,
    )
    envelope["metrics"]["v1V2DisagreementRateByDisposition"] = disagreement_metrics(records)
    try:
        camera_catalog = load_faa_catalog()
        if not camera_catalog:
            raise ValueError("FAA camera catalog is empty")
        research_review = select_research_candidates(records, camera_catalog=camera_catalog)
    except Exception as error:
        print(f"[research-review] selection failed: {str(error)[:300]}")
        research_review = {"ok": False, "operationalImpact": False, "error": str(error)[:300], "selected": 0}
    envelope["shadowV2"] = {
        "methodVersion": METHOD_VERSION, "status": "complete" if not result.get("errors") else "complete_with_errors",
        "assessmentProcessingAt": result.get("assessmentProcessingAt"),
        "enrichmentLatencySeconds": result.get("enrichmentLatencySeconds"),
        "enrichedRecords": result.get("enriched", 0), "errorCount": len(result.get("errors") or []),
        "researchReview": research_review,
    }
    body, content_hash = encoded(envelope)
    s3.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/json", ContentEncoding="gzip",
        ServerSideEncryption="AES256", Metadata={
            "uncompressed-sha256": content_hash, "schema-version": SCHEMA_VERSION,
            "detector-rule-version": envelope.get("detectorRuleVersion") or "unknown", "sunlight-v2-method-version": METHOD_VERSION,
        },
    )
    review_callback = safe_push_review_assessments(envelope)
    return {"ok": True, "bucket": bucket, "key": key, "researchReview": research_review,
            "reviewCallback": review_callback, **result}
