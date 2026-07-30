"""Asynchronous private Opportunity Ledger Lambda entry point."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ledger_review import load_faa_catalog, select_ledger_review_records
from opportunity_ledger import build_opportunity_ledger
from opportunity_ledger_store import load_previous, persist
from review_callback import safe_push_review_assessments
from sunlight_v2 import enrich_sunlight_v2


def _sunlight_candidate(record: dict) -> dict:
    features = record["features"]
    observer, rain, geometry = features["observer"], features["rain"], features["geometry"]
    return {"lat": observer["lat"], "lon": observer["lon"], "rainLat": rain.get("lat"), "rainLon": rain.get("lon"),
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
    stored = persist(ledger, os.environ.get("RAINBOW_RESEARCH_BUCKET", bucket), s3)
    # This is a private, post-publication lane. Only persistent ledger swaths
    # with an actually useful camera can consume one of two research slots.
    review_selection = select_ledger_review_records(ledger, previous, sidecar, load_faa_catalog())
    records = review_selection.pop("records")
    if records:
        enrich_sunlight_v2(records, [_sunlight_candidate(record) for record in records], ledger["scanTime"],
                           Path(os.environ.get("RAINBOW_CACHE_DIR", "/tmp/rainbow-ledger-review")),
                           assessment_processing_at=ledger["generatedAt"])
    review_callback = safe_push_review_assessments({
        "detectorRuleVersion": review_selection["ruleVersion"],
        "generatedAt": ledger["generatedAt"],
        "radar": {"observedAt": ledger["scanTime"], "rainFootprintId": ledger["rainFootprintId"],
                  "rainFootprintContentSha256": actual_hash},
        "records": records,
    })
    return {
        "ok": True, "scanTime": ledger["scanTime"], "runtimeMs": round((time.time() - started) * 1000),
        "rainEvents": ledger["stats"]["rainEvents"], "opportunities": ledger["stats"]["opportunities"],
        "observerSwathCells": ledger["stats"]["observerSwathCells"], "storage": stored,
        "reviewSelection": review_selection, "reviewCallback": review_callback,
    }
