"""Asynchronous private Opportunity Ledger Lambda entry point."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ledger_review import load_faa_catalog
from opportunity_ledger import build_opportunity_ledger
from opportunity_ledger_store import load_previous, persist
from review_callback import safe_push_review_assessments
from sunlight_v2 import enrich_sunlight_v2
from v4_shadow_review import finalize_v42_records, mark_v42_dispatched, select_v4_shadow_records
from v5_shadow_priority import compact_v5_feed, score_v5_shadow_records
from v5_review_feed import safe_push_v5_review_feed


def _sunlight_candidate(record: dict) -> dict:
    features = record["features"]
    observer, rain, geometry = features["observer"], features["rain"], features["geometry"]
    return {"candidateId": record["candidateId"], "lat": observer["lat"], "lon": observer["lon"], "rainLat": rain.get("lat"), "rainLon": rain.get("lon"),
            "rainDistanceKm": rain.get("distanceKm"), "rainRateMmHr": rain.get("rateMmHr"),
            "observerRainRateMmHr": rain.get("observerRateMmHr"), "sunElevationDeg": geometry.get("sunElevationDeg"),
            "sunBearingDeg": geometry.get("sunBearingDeg"), "antiSolarBearingDeg": geometry.get("antiSolarBearingDeg"),
            "radarScore": geometry.get("radarScore")}


def handler(event, context):
    started = time.time()
    bucket, footprint_key = event["bucket"], event["key"]
    import boto3
    s3 = boto3.client("s3")
    compressed = s3.get_object(Bucket=bucket, Key=footprint_key)["Body"].read()
    raw = gzip.decompress(compressed)
    actual_hash = hashlib.sha256(raw).hexdigest()
    expected_hash = event.get("contentSha256")
    if expected_hash and actual_hash != expected_hash:
        raise ValueError("rain footprint content hash mismatch")
    sidecar = json.loads(raw)
    previous, previous_key = load_previous(bucket, sidecar["observedAt"], s3)
    ledger = build_opportunity_ledger(sidecar, previous=previous)
    now = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(sidecar["observedAt"].replace("Z", "+00:00"))
    ledger["generatedAt"] = now.isoformat().replace("+00:00", "Z")
    ledger["sourceRainFootprint"] = {
        "bucket": bucket, "key": footprint_key, "contentSha256": actual_hash,
    }
    ledger["previousLedgerKey"] = previous_key
    ledger["generationLagSeconds"] = round((now - observed).total_seconds(), 1)
    catalog = load_faa_catalog()
    review_selection = select_v4_shadow_records(sidecar, previous, catalog)
    records = review_selection.pop("records")
    v4_state = review_selection.pop("state")
    # Evaluate and retain every eligible family in S3 even when the Review
    # projection is disabled or temporarily unavailable.
    if records:
        enrich_sunlight_v2(records, [_sunlight_candidate(record) for record in records], ledger["scanTime"],
                           Path(os.environ.get("RAINBOW_CACHE_DIR", "/tmp/rainbow-ledger-review")),
                           assessment_processing_at=ledger["generatedAt"])
    finalized = finalize_v42_records(records, v4_state, ledger["generatedAt"])
    records = finalized["records"]
    v5_selection = score_v5_shadow_records(
        sidecar, records, previous, ledger["scanTime"], v4_state["stormLedger"],
    )
    v5_state = v5_selection.pop("state")
    v5_feed = compact_v5_feed(v5_selection)
    v5_selection.pop("records", None)
    ledger["v5ShadowState"] = v5_state
    ledger["v5ShadowPrediction"] = v5_selection
    current_family_days = {
        (record["features"]["researchReview"]["familyEventId"], ledger["generatedAt"][:10])
        for record in records
    }
    backlog = {}
    for record in v4_state.get("projectionBacklog") or []:
        review = (record.get("features") or {}).get("researchReview") or {}
        prediction = review.get("v4Prediction") or {}
        key = (review.get("familyEventId"), str(prediction.get("assessmentProcessingAt") or "")[:10])
        if key not in current_family_days:
            backlog[key] = record
    for record in finalized["dispatchRecords"]:
        review = record["features"]["researchReview"]
        backlog[(review["familyEventId"], ledger["generatedAt"][:10])] = record
    dispatch_records = list(backlog.values())
    v4_state["projectionBacklog"] = dispatch_records
    review_selection["classificationCounts"] = finalized["classificationCounts"]
    review_selection["dispatchSelected"] = len(dispatch_records)
    ledger["v4ShadowState"] = v4_state
    ledger["v4ShadowPrediction"] = {**review_selection, "records": records}
    stored = persist(ledger, os.environ.get("RAINBOW_RESEARCH_BUCKET", bucket), s3)
    v5_review_feed = safe_push_v5_review_feed(v5_feed)
    # The callback URL is only a projection kill switch; it never disables the
    # authoritative evaluation and retention path above.
    if not str(os.environ.get("RAINBOW_REVIEW_ENRICH_URL") or "").strip():
        review_selection.update({"ok": True, "skipped": True, "reason": "V4 shadow review callback disabled"})
        review_callback = {"ok": True, "skipped": True, "reason": "ledger review lane disabled", "assessments": 0}
    else:
        review_callback = safe_push_review_assessments({
            "detectorRuleVersion": review_selection["ruleVersion"], "generatedAt": ledger["generatedAt"],
            "radar": {"observedAt": ledger["scanTime"], "rainFootprintId": ledger["rainFootprintId"],
                      "rainFootprintContentSha256": actual_hash}, "records": dispatch_records,
        })
        response = review_callback.get("response") or {}
        projected = int(response.get("attached") or 0) + int(response.get("duplicates") or 0)
        if review_callback.get("ok") and dispatch_records and projected == len(dispatch_records):
            mark_v42_dispatched(v4_state, dispatch_records, ledger["generatedAt"])
            v4_state["projectionBacklog"] = []
            ledger["v4ShadowState"] = v4_state
            stored = persist(ledger, os.environ.get("RAINBOW_RESEARCH_BUCKET", bucket), s3)
        elif dispatch_records:
            review_selection["projectionPending"] = len(dispatch_records)
    return {
        "ok": True, "scanTime": ledger["scanTime"], "runtimeMs": round((time.time() - started) * 1000),
        "rainEvents": ledger["stats"]["rainEvents"], "opportunities": ledger["stats"]["opportunities"],
        "observerSwathCells": ledger["stats"]["observerSwathCells"], "storage": stored,
        "reviewSelection": review_selection, "reviewCallback": review_callback,
        "v5ReviewFeed": v5_review_feed,
    }
