#!/usr/bin/env python3
"""Replay worker V1 and V2 sunlight against a bounded historical FAA case pool.

This is a local study driver. It uses archived GOES and IEM ASOS inputs, pins
assessment time, and never calls Redis, callbacks, or the production review API.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
WORKER_DIR = ROOT / "worker"
for directory in (API_DIR, WORKER_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from decision_log import RULE_VERSION, SUNLIGHT_V2_METHOD_VERSION, build_record, candidate_id
from sunlight_decision import build_decision, collect_sources
from sunlight_v2 import METHOD_VERSION, enrich_sunlight_v2, parse_utc, v1_sunlight_category

IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_METAR_METHOD_VERSION = "iem-asos-archive-replay-v1"
STUDY_SCHEMA_VERSION = "historical-sunlight-replay.v1"
FIXED_LATENCY_SECONDS = 8 * 60


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def historical_candidate(item: dict) -> dict:
    site = item["site"]
    return {
        "lat": float(site["latitude"]),
        "lon": float(site["longitude"]),
        "sunElevationDeg": float(item["sunElevation"]),
        "sunBearingDeg": float(item["sunAzimuth"]),
        "antiSolarBearingDeg": float(item["antiSolarAzimuth"]),
        "rainLat": float(site["latitude"]),
        "rainLon": float(site["longitude"]),
        "rainDistanceKm": 0.0,
        "rainRateMmHr": float(item.get("rainInches") or 0) * 25.4,
        "observerRainRateMmHr": float(item.get("rainInches") or 0) * 25.4,
        "radarScore": float(item.get("metadataScore") or 0),
        "sourceCaseId": f"faa-{site['siteId']}-{item['observedAt']}",
    }


def station_catalog(path: Path) -> dict[str, dict]:
    rows = load_json(path)
    return {str(site.get("siteIdentifier")): site for site in rows if site.get("siteIdentifier")}


def fetch_archived_asos(catalog: dict[str, dict], start: datetime, end: datetime, session: requests.Session) -> list[dict]:
    stations = sorted(catalog)
    output = []
    for offset in range(0, len(stations), 70):
        chunk = stations[offset:offset + 70]
        params = [("station", station) for station in chunk]
        params += [("data", field) for field in ("skyc1", "skyl1", "skyc2", "skyl2", "skyc3", "skyl3", "skyc4", "skyl4", "wxcodes")]
        params += [("report_type", "3"), ("sts", iso(start)), ("ets", iso(end)), ("tz", "Etc/UTC"), ("format", "onlycomma"), ("missing", "empty")]
        response = session.get(IEM_ASOS_URL, params=params, timeout=300)
        response.raise_for_status()
        rows = list(csv.DictReader(response.text.splitlines()))
        for row in rows:
            station = catalog.get(row.get("station"))
            if not station:
                continue
            try:
                observed = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            layers = []
            for index in range(1, 5):
                cover, base = row.get(f"skyc{index}"), row.get(f"skyl{index}")
                if cover:
                    try:
                        base_ft = float(base) * 100 if base else None
                    except ValueError:
                        base_ft = None
                    layers.append({"cover": cover, "baseFtAgl": base_ft})
            output.append({
                "stationId": row.get("station"), "observedAt": iso(observed),
                "lat": float(station["latitude"]), "lon": float(station["longitude"]),
                "weather": row.get("wxcodes") or None, "cloudLayers": layers,
                "rawText": f"IEM ASOS {row.get('station')} {row.get('valid')} {row.get('wxcodes') or ''}".strip(),
            })
    return output


def fetch_historical_model(candidate: dict, observed_at: str, session: requests.Session) -> dict:
    when = parse_utc(observed_at)
    date = when.date().isoformat()
    response = session.get(
        "https://historical-forecast-api.open-meteo.com/v1/forecast",
        params={
            "latitude": candidate["lat"], "longitude": candidate["lon"],
            "start_date": date, "end_date": date,
            "hourly": "cloud_cover,direct_normal_irradiance_instant",
            "timezone": "GMT",
        }, timeout=30,
    )
    response.raise_for_status()
    hourly = response.json().get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise RuntimeError("Historical Open-Meteo returned no hourly rows")
    index = min(range(len(times)), key=lambda i: abs((datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc) - when).total_seconds()))
    dni = (hourly.get("direct_normal_irradiance_instant") or [None])[index]
    cloud = (hourly.get("cloud_cover") or [None])[index]
    return {
        "directNormalIrradianceWm2": dni, "cloudCoverPct": cloud,
        "observedAt": times[index] + ":00Z", "available": True,
        "methodVersion": "open-meteo-historical-forecast-replay-v1",
        "approximate": True,
    }

def load_reviewed_exclusions(paths: list[Path]) -> list[dict]:
    """Map reviewed candidate numbers back to ranked batch event metadata."""
    exclusions = []
    for ranked_path, labels_path in paths:
        if not ranked_path.exists() or not labels_path.exists():
            continue
        ranked = load_json(ranked_path)
        labels = load_json(labels_path).get("labels", [])
        for label in labels:
            try:
                number = int(str(label["candidateId"]).split("-")[-1]) - 1
                item = ranked[number]
            except (KeyError, IndexError, ValueError):
                continue
            exclusions.append({"siteId": item["site"]["siteId"], "lat": item["site"]["latitude"], "lon": item["site"]["longitude"], "observedAt": item["observedAt"], "source": str(labels_path)})
    return exclusions


def overlaps_exclusion(item: dict, exclusions: list[dict]) -> bool:
    site = item["site"]
    when = parse_utc(item["observedAt"])
    for exclusion in exclusions:
        if exclusion.get("siteId") != site.get("siteId"):
            continue
        if abs((when - parse_utc(exclusion["observedAt"])).total_seconds()) <= 3 * 3600:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranked", type=Path, required=True)
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip-v2", action="store_true", help="Build V1 satellite-only output without large Band-2 downloads")
    args = parser.parse_args()

    items = load_json(args.ranked)
    items = [item for item in items if parse_utc(item["observedAt"]) < parse_utc("2026-07-29T00:00:00Z")]
    exclusion_paths = [
        (ROOT / "validation/faa/faa-ranked-pilot.json", ROOT / "validation/faa/faa-human-labels.json"),
        (ROOT / "validation/faa/faa-ranked-expansion.json", ROOT / "validation/faa/faa-expansion-human-labels.json"),
        (ROOT / "validation/faa/faa-ranked-network-expansion.json", ROOT / "validation/faa/faa-network-expansion-human-labels.json"),
    ]
    exclusions = load_reviewed_exclusions(exclusion_paths)
    items = [item for item in items if not overlaps_exclusion(item, exclusions)]
    if args.limit:
        items = items[:args.limit]
    if not items:
        raise SystemExit("No replayable cases remain after cutoff and reviewed-event exclusions")

    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    cache = output / "cache"
    output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    start = min(parse_utc(item["observedAt"]) for item in items) - timedelta(hours=2)
    end = max(parse_utc(item["observedAt"]) for item in items) + timedelta(hours=2)
    catalog = station_catalog(args.sites)
    needed_stations = {str(item["site"].get("siteIdentifier")) for item in items}
    catalog = {key: value for key, value in catalog.items() if key in needed_stations}
    metars = fetch_archived_asos(catalog, start, end, session)
    results = []
    for number, item in enumerate(items, 1):
        radar_at = item["observedAt"]
        assessment_at = iso(parse_utc(radar_at) + timedelta(seconds=FIXED_LATENCY_SECONDS))
        candidate = historical_candidate(item)
        sources = collect_sources(candidate["lat"], candidate["lon"], "auto", 3, cache / "v1-goes", False, observed_at=radar_at, available_by=assessment_at)
        decision = build_decision(sources)
        record = build_record(candidate, radar_at, "historical_candidate", "historical_replay", ["faa_pre_review_pool"], {"direct_watch_min": 120, "direct_go_min": 200, "fallback_radar_min": 90, "fallback_rain_distance_max": 15, "fallback_observer_rain_max": 0.05, "optimal_sun_min": 5, "optimal_sun_max": 22}, decision, sources)
        v1_satellite = {"variant": "satellite-only", "methodVersion": RULE_VERSION, "sunlightCategory": v1_sunlight_category(record), "decision": decision, "sources": sources, "approximate": False}
        full_record = json.loads(json.dumps(record))
        model = fetch_historical_model(candidate, radar_at, session)
        full_record["features"]["model"] = model
        v1_full = {"variant": "full-approximate", "methodVersion": RULE_VERSION, "sunlightCategory": v1_sunlight_category(full_record), "model": model, "approximate": True}
        row = {"caseId": candidate["sourceCaseId"], "observedAt": radar_at, "assessmentProcessingAt": assessment_at, "fixedLatencySeconds": FIXED_LATENCY_SECONDS, "inputFidelity": "FAA-site-time sunlight point; rain geometry inherited from candidate metadata", "v1": {"satelliteOnly": v1_satellite, "fullApproximate": v1_full}, "v2": None, "humanReview": None, "sourceFrameFiles": [frame.get("file") for frame in item.get("frames", [])], "sourceArtifacts": {"ranked": str(args.ranked), "rankedSha256": sha256(args.ranked)}, "excludedReviewedEvents": len(exclusions)}
        if not args.skip_v2:
            v2_result = enrich_sunlight_v2([full_record], [candidate], radar_at, cache / "v2", session=session, assessment_processing_at=assessment_at, metars=metars, metar_method_version=IEM_METAR_METHOD_VERSION)
            row["v2"] = full_record.get("features", {}).get("sunlightV2")
            row["v2Run"] = {key: v2_result.get(key) for key in ("ok", "enriched", "errors", "assessmentProcessingAt", "methodVersion")}
        results.append(row)
        print(f"Replayed {number}/{len(items)}: {row['caseId']} V1={v1_satellite['sunlightCategory']} V2={'done' if row['v2'] else 'skipped'}", flush=True)
    manifest = {"schemaVersion": STUDY_SCHEMA_VERSION, "generatedAt": iso(datetime.now(timezone.utc)), "window": {"start": iso(start), "endExclusive": iso(end), "productionReviewCutoff": "2026-07-29T00:00:00Z"}, "ruleVersion": RULE_VERSION, "v2MethodVersion": SUNLIGHT_V2_METHOD_VERSION, "metarMethodVersion": IEM_METAR_METHOD_VERSION, "fixedLatencySeconds": FIXED_LATENCY_SECONDS, "caseCount": len(results), "reviewedExclusionCount": len(exclusions), "cases": results}
    (output / "replay-manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output / "replay-manifest.json"), "cases": len(results), "metars": len(metars), "v2": not args.skip_v2}, indent=2))


if __name__ == "__main__":
    main()
