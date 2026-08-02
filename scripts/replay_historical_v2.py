#!/usr/bin/env python3
"""Run grouped historical V2 enrichment over a completed V1 replay manifest."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "api", ROOT / "worker"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from decision_log import build_record
from replay_historical_sunlight import fetch_archived_asos, historical_candidate, iso, parse_utc, station_catalog, IEM_METAR_METHOD_VERSION
from sunlight_v2 import enrich_sunlight_v2


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-manifest", type=Path, required=True)
    parser.add_argument("--ranked", type=Path, required=True)
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    v1_manifest = load_json(args.v1_manifest)
    ranked = load_json(args.ranked)
    by_case = {case["caseId"]: case for case in v1_manifest["cases"]}
    items = [item for item in ranked if f"faa-{item['site']['siteId']}-{item['observedAt']}" in by_case]
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit("No V1 cases matched the ranked input")

    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "cache"
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    start = min(parse_utc(item["observedAt"]) for item in items) - timedelta(hours=2)
    end = max(parse_utc(item["observedAt"]) for item in items) + timedelta(hours=2)
    needed = {str(item["site"].get("siteIdentifier")) for item in items}
    catalog = {key: value for key, value in station_catalog(args.sites).items() if key in needed}
    metars = fetch_archived_asos(catalog, start, end, session)

    groups = defaultdict(list)
    for item in items:
        groups[item["observedAt"]].append(item)
    updated = {case["caseId"]: dict(case) for case in v1_manifest["cases"]}
    for number, (radar_at, group) in enumerate(sorted(groups.items()), 1):
        records, candidates = [], []
        for item in group:
            case_id = f"faa-{item['site']['siteId']}-{item['observedAt']}"
            old = by_case[case_id]
            candidate = historical_candidate(item)
            satellite = old["v1"]["satelliteOnly"]
            decision, sources = satellite["decision"], satellite["sources"]
            record = build_record(candidate, radar_at, "historical_candidate", "historical_replay", ["faa_pre_review_pool"], {"direct_watch_min": 120, "direct_go_min": 200, "fallback_radar_min": 90, "fallback_rain_distance_max": 15, "fallback_observer_rain_max": 0.05, "optimal_sun_min": 5, "optimal_sun_max": 22}, decision, sources)
            record["features"]["model"] = old["v1"]["fullApproximate"]["model"]
            records.append(record)
            candidates.append(candidate)
        group_dir = cache / radar_at.replace(":", "").replace("-", "")
        assessment_at = iso(parse_utc(radar_at) + timedelta(seconds=v1_manifest["fixedLatencySeconds"]))
        result = enrich_sunlight_v2(records, candidates, radar_at, group_dir, session=session, assessment_processing_at=assessment_at, metars=metars, metar_method_version=IEM_METAR_METHOD_VERSION)
        for item, record in zip(group, records):
            case_id = f"faa-{item['site']['siteId']}-{item['observedAt']}"
            updated[case_id]["v2"] = record.get("features", {}).get("sunlightV2")
            updated[case_id]["v2Run"] = {key: result.get(key) for key in ("ok", "enriched", "errors", "assessmentProcessingAt", "methodVersion")}
        print(f"V2 group {number}/{len(groups)} at {radar_at}: {len(group)} cases, enriched={result.get('enriched')} errors={len(result.get('errors') or [])}", flush=True)

    output_manifest = dict(v1_manifest)
    output_manifest["v2GroupedReplay"] = True
    output_manifest["v2Cases"] = sum(1 for case in updated.values() if case.get("v2"))
    output_manifest["cases"] = [updated[case["caseId"]] for case in v1_manifest["cases"]]
    (output / "replay-manifest-v2.json").write_text(json.dumps(output_manifest, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output / "replay-manifest-v2.json"), "groups": len(groups), "cases": len(items), "metars": len(metars)}, indent=2))


if __name__ == "__main__":
    main()
