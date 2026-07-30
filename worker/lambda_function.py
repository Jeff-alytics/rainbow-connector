"""AWS Lambda entry point for the NOAA-first detector."""

from __future__ import annotations

import os
from pathlib import Path

from pipeline import build_final_artifact, capture_dot_evidence, notify_subscribers, publish_artifact
from decision_store import safe_persist_decision_log
from rain_footprint_store import safe_build_and_persist_rain_footprint
from shadow_dispatch import safe_invoke_sunlight_v2
from map_tier_dispatch import safe_invoke_map_tiers
from opportunity_ledger_dispatch import safe_invoke_opportunity_ledger


def handler(event, context):
    maximum = int(os.environ.get("RAINBOW_MAX_CANDIDATES", "250"))
    stride = int(os.environ.get("RAINBOW_MRMS_STRIDE", "10"))
    cache_dir = Path(os.environ.get("RAINBOW_CACHE_DIR", "/tmp/rainbow-worker"))
    decision_records = []
    research_artifacts = {}
    artifact = build_final_artifact(
        cache_dir, maximum=maximum, stride=stride, decision_records=decision_records, research_artifacts=research_artifacts,
    )
    stored = publish_artifact(artifact)
    notified = notify_subscribers()
    # Research logging is deliberately after the live artifact and subscriber
    # notification. Its failure is visible but never blocks either path.
    rain_footprint = safe_build_and_persist_rain_footprint(
        research_artifacts.get("rainFootprintContext"), decision_records=decision_records,
    )
    map_tiers = safe_invoke_map_tiers(rain_footprint)
    opportunity_ledger = safe_invoke_opportunity_ledger(rain_footprint)
    if rain_footprint.get("contentSha256"):
        artifact.setdefault("sourceHealth", {}).setdefault("radar", {})["rainFootprintContentSha256"] = rain_footprint["contentSha256"]
    decision_log = safe_persist_decision_log(artifact, decision_records)
    sunlight_v2 = safe_invoke_sunlight_v2(decision_log, len(decision_records))
    try:
        dot_evidence = capture_dot_evidence()
    except Exception as error:
        # Camera collection is supporting review evidence. It must never prevent
        # a healthy forecast artifact or subscriber alert from completing.
        dot_evidence = {"ok": False, "error": str(error)[:300]}
    return {
        "ok": True,
        "generatedAt": artifact["generatedAt"],
        "runtimeMs": artifact.get("runtimeMs"),
        "candidates": len(artifact.get("candidates") or []),
        "possibleCandidates": len(artifact.get("possibleCandidates") or []),
        "storage": stored,
        "notifications": notified,
        "decisionLog": decision_log,
        "rainFootprint": rain_footprint,
        "mapTiers": map_tiers,
        "opportunityLedger": opportunity_ledger,
        "sunlightV2": sunlight_v2,
        "dotEvidence": dot_evidence,
    }
