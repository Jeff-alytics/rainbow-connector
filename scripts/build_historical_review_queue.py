#!/usr/bin/env python
"""Build the historical review queue from the MRMS candidate pool.

Stages:
  A. Dedupe pooled camera-matched seeds into site events (gap <= 30 min).
  B. Apply event-level exclusions (frozen ledger cases by window+radius,
     already-reviewed FAA batches by site+time).
  C. Replay production V1 on each event's representative scan:
     satellite lane exactly (archived GOES via the shimmed collect_sources),
     model lane approximately (Open-Meteo Historical Forecast substitute).
  D. Assign the production disposition (strict_go / possible / rejected) with
     the production constants, add a labelled near-miss band, rank, and write
     the queue. No FAA images are downloaded here.

Everything stays under validation/; no production store, Redis, or callback.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "worker", ROOT / "api"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import goes_sample
from decision_log import RULE_VERSION, build_record
from enrichment import (DIRECT_GO_MIN, DIRECT_WATCH_MIN, OPTIMAL_SUN_MAX, OPTIMAL_SUN_MIN,
                        WATCH_SUN_MAX, WATCH_SUN_MIN)
from sunlight_decision import build_decision, collect_sources
from sunlight_v2 import parse_utc, v1_sunlight_category

SCHEMA_VERSION = "historical-review-queue.v1"
FIXED_LATENCY_SECONDS = 8 * 60
EVENT_GAP_MINUTES = 30
NEAR_MISS_DNI_MIN = 80.0
FROZEN_EXCLUSION_RADIUS_KM = 100.0
FROZEN_EXCLUSION_PAD_HOURS = 3.0
REVIEWED_EXCLUSION_RADIUS_KM = 60.0
REVIEWED_EXCLUSION_PAD_HOURS = 3.0
MODEL_METHOD_VERSION = "open-meteo-historical-forecast-replay-v1"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlon = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


# ---------------------------------------------------------------- Stage A

def load_pool_rows(pool_dir: Path) -> list[dict]:
    rows = []
    for day_file in sorted(pool_dir.glob("pool-*.jsonl")):
        for line in day_file.read_text(encoding="utf-8").splitlines():
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("status") != "ok":
                continue
            for seed in frame.get("matched", []):
                rows.append({**seed, "observedAt": frame["observedAt"]})
    return rows


def build_events(rows: list[dict]) -> list[dict]:
    """Group camera-matched seeds into per-site events split on 30-min gaps."""
    by_site: dict[str, list[dict]] = {}
    for row in rows:
        primary = row["cameraMatches"][0]
        by_site.setdefault(str(primary["siteId"]), []).append(row)
    events = []
    for site_id, site_rows in by_site.items():
        site_rows.sort(key=lambda item: item["observedAt"])
        current: list[dict] = []
        for row in site_rows:
            if current and (parse_utc(row["observedAt"]) - parse_utc(current[-1]["observedAt"])
                            ).total_seconds() > EVENT_GAP_MINUTES * 60:
                events.append(finish_event(site_id, current))
                current = []
            current.append(row)
        if current:
            events.append(finish_event(site_id, current))
    return sorted(events, key=lambda event: event["startAt"])


def finish_event(site_id: str, rows: list[dict]) -> dict:
    scans = sorted({row["observedAt"] for row in rows})
    best = max(rows, key=lambda row: (row.get("radarScore") or 0, row["observedAt"]))
    cameras: dict[str, dict] = {}
    for row in rows:
        for match in row["cameraMatches"]:
            if str(match["siteId"]) != site_id:
                continue
            key = str(match["cameraId"])
            kept = cameras.get(key)
            if kept is None or match["visibleBowFraction"] > kept["visibleBowFraction"]:
                cameras[key] = match
    primary = best["cameraMatches"][0]
    return {
        "eventId": f"site{site_id}-{scans[0].replace(':', '').replace('-', '')}",
        "siteId": site_id, "siteName": primary.get("name"),
        "startAt": scans[0], "endAt": scans[-1], "scanCount": len(scans),
        "persistent": len(scans) >= 2,
        "bestScan": {key: best[key] for key in
                     ("observedAt", "lat", "lon", "rainLat", "rainLon", "rainDistanceKm",
                      "rainRateMmHr", "observerRainRateMmHr", "sunElevationDeg", "sunBearingDeg",
                      "antiSolarBearingDeg", "radarScore")},
        "cameras": sorted(cameras.values(), key=lambda cam: -cam["visibleBowFraction"]),
        "scanTimes": scans,
    }


# ---------------------------------------------------------------- Stage B

def frozen_exclusions() -> list[dict]:
    fixture = json.loads((ROOT / "validation/opportunity-ledger/case-fixture-v1.json")
                         .read_text(encoding="utf-8-sig"))
    out = []
    for case in fixture.get("cases", []):
        start, end = case["window"]
        for observation in case.get("observations", []):
            out.append({"source": f"frozen:{case['id']}", "lat": observation["lat"],
                        "lon": observation["lon"], "startAt": start, "endAt": end,
                        "radiusKm": FROZEN_EXCLUSION_RADIUS_KM,
                        "padHours": FROZEN_EXCLUSION_PAD_HOURS})
    return out


def reviewed_exclusions() -> list[dict]:
    pairs = [
        ("validation/faa/faa-ranked-pilot.json", "validation/faa/faa-human-labels.json"),
        ("validation/faa/faa-ranked-expansion.json", "validation/faa/faa-expansion-human-labels.json"),
        ("validation/faa/faa-ranked-network-expansion.json", "validation/faa/faa-network-expansion-human-labels.json"),
    ]
    out = []
    for ranked_name, labels_name in pairs:
        ranked_path, labels_path = ROOT / ranked_name, ROOT / labels_name
        if not ranked_path.exists() or not labels_path.exists():
            continue
        ranked = json.loads(ranked_path.read_text(encoding="utf-8-sig"))
        labels_doc = json.loads(labels_path.read_text(encoding="utf-8-sig"))
        labels = labels_doc.get("labels", labels_doc if isinstance(labels_doc, list) else [])
        for label in labels:
            try:
                index = int(str(label["candidateId"]).split("-")[-1]) - 1
                item = ranked[index]
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            site = item.get("site") or {}
            lat, lon = site.get("latitude"), site.get("longitude")
            when = item.get("observedAt")
            if lat is None or lon is None or not when:
                continue
            out.append({"source": f"reviewed:{labels_path.name}", "lat": float(lat),
                        "lon": float(lon), "startAt": when, "endAt": when,
                        "radiusKm": REVIEWED_EXCLUSION_RADIUS_KM,
                        "padHours": REVIEWED_EXCLUSION_PAD_HOURS})
    return out


def excluded_by(event: dict, exclusions: list[dict]) -> str | None:
    start = parse_utc(event["startAt"])
    end = parse_utc(event["endAt"])
    lat, lon = event["bestScan"]["lat"], event["bestScan"]["lon"]
    for rule in exclusions:
        pad = timedelta(hours=rule["padHours"])
        if end < parse_utc(rule["startAt"]) - pad or start > parse_utc(rule["endAt"]) + pad:
            continue
        if distance_km(lat, lon, rule["lat"], rule["lon"]) <= rule["radiusKm"]:
            return rule["source"]
    return None


# ---------------------------------------------------------------- Stage C

def fetch_historical_model(lat: float, lon: float, observed_at: str, session: requests.Session) -> dict:
    """Open-Meteo Historical Forecast substitute for the live `current` call.

    Approximately what production's worker/enrichment.py would have seen; the
    exact interpolated `current` values at the original moment are
    unrecoverable, so every consumer must treat this lane as approximate.
    """
    when = parse_utc(observed_at)
    date = when.date().isoformat()
    for attempt in range(3):
        response = session.get(
            "https://historical-forecast-api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "start_date": date, "end_date": date,
                    "hourly": "cloud_cover,direct_normal_irradiance_instant", "timezone": "GMT"},
            timeout=30)
        if response.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        response.raise_for_status()
        break
    hourly = response.json().get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        raise RuntimeError("Historical Open-Meteo returned no hourly rows")
    index = min(range(len(times)),
                key=lambda i: abs((datetime.fromisoformat(times[i]).replace(tzinfo=timezone.utc)
                                   - when).total_seconds()))
    return {"directNormalIrradianceWm2": (hourly.get("direct_normal_irradiance_instant") or [None])[index],
            "cloudCoverPct": (hourly.get("cloud_cover") or [None])[index],
            "observedAt": times[index] + ":00Z", "available": True,
            "methodVersion": MODEL_METHOD_VERSION, "approximate": True}


def classify_disposition(sun_elevation_deg: float, dni_wm2: float, goes_decision: str | None) -> str:
    """Production V1 selection rules (worker/enrichment.py) plus a labelled near-miss band."""
    strict_go = (OPTIMAL_SUN_MIN <= sun_elevation_deg <= OPTIMAL_SUN_MAX
                 and dni_wm2 >= DIRECT_GO_MIN and goes_decision == "go")
    possible = (WATCH_SUN_MIN <= sun_elevation_deg <= WATCH_SUN_MAX
                and dni_wm2 >= DIRECT_WATCH_MIN and goes_decision in ("go", "watch"))
    near_miss = (not strict_go and not possible
                 and ((goes_decision in ("go", "watch")
                       and NEAR_MISS_DNI_MIN <= dni_wm2 < DIRECT_WATCH_MIN)
                      or (goes_decision == "unknown" and dni_wm2 >= DIRECT_GO_MIN)))
    return "go" if strict_go else "possible" if possible else \
           "near_miss" if near_miss else "rejected"


def replay_event(event: dict, cache_dir: Path, session: requests.Session) -> dict:
    scan = event["bestScan"]
    observed_at = scan["observedAt"]
    available_by = iso(parse_utc(observed_at) + timedelta(seconds=FIXED_LATENCY_SECONDS))
    sources = collect_sources(scan["lat"], scan["lon"], "auto", 3, cache_dir, False,
                              observed_at=observed_at, available_by=available_by)
    decision = build_decision(sources)

    candidate = {key: scan[key] for key in
                 ("lat", "lon", "rainLat", "rainLon", "rainDistanceKm", "rainRateMmHr",
                  "observerRainRateMmHr", "sunElevationDeg", "sunBearingDeg",
                  "antiSolarBearingDeg", "radarScore")}
    constants = {"direct_watch_min": DIRECT_WATCH_MIN, "direct_go_min": DIRECT_GO_MIN,
                 "fallback_radar_min": 90, "fallback_rain_distance_max": 15,
                 "fallback_observer_rain_max": 0.05,
                 "optimal_sun_min": OPTIMAL_SUN_MIN, "optimal_sun_max": OPTIMAL_SUN_MAX}
    record = build_record(candidate, observed_at, "historical_replay", "historical_replay",
                          ["historical_candidate_pool"], constants, decision, sources)
    satellite_only_category = v1_sunlight_category(record)

    model_error = None
    try:
        model = fetch_historical_model(scan["lat"], scan["lon"], observed_at, session)
    except Exception as error:
        model, model_error = None, str(error)[:200]
    dni = (model or {}).get("directNormalIrradianceWm2")
    dni = float(dni) if dni is not None else 0.0
    elevation = scan["sunElevationDeg"]
    goes_decision = decision.get("sunlightDecision")
    disposition = classify_disposition(elevation, dni, goes_decision)
    if model is not None:
        record["features"]["model"] = model
    full_category = v1_sunlight_category(record)
    return {
        **event,
        "assessmentProcessingAt": available_by, "fixedLatencySeconds": FIXED_LATENCY_SECONDS,
        "v1": {
            "satelliteOnly": {"sunlightCategory": satellite_only_category,
                              "decision": decision, "approximate": False},
            "fullApproximate": {"sunlightCategory": full_category, "model": model,
                                "modelError": model_error, "approximate": True},
            "ruleVersion": RULE_VERSION,
        },
        "disposition": disposition,
        "dispositionInputs": {"dniWm2": dni, "sunElevationDeg": elevation,
                              "goesDecision": goes_decision,
                              "dniSource": MODEL_METHOD_VERSION if model else "unavailable"},
    }


# ---------------------------------------------------------------- Stage D

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-dir", type=Path, default=ROOT / "validation/historical-candidate-pool")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation/historical-review-queue")
    parser.add_argument("--limit-events", type=int, default=0, help="replay only the first N events (test)")
    parser.add_argument("--min-scan-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1,
                        help="run events where index %% shard-count == shard-index; each shard "
                             "writes its own events file and GOES cache so shards never collide")
    args = parser.parse_args()

    pool_dir = args.pool_dir if args.pool_dir.is_absolute() else ROOT / args.pool_dir
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_suffix = f"-shard{args.shard_index}" if args.shard_count > 1 else ""
    cache_dir = output_dir / f"goes-cache{shard_suffix}"

    rows = load_pool_rows(pool_dir)
    events = build_events(rows)
    print(f"pool: {len(rows)} camera-matched seed-scans -> {len(events)} site events", flush=True)

    exclusions = frozen_exclusions() + reviewed_exclusions()
    kept, excluded = [], []
    for event in events:
        reason = excluded_by(event, exclusions)
        if reason:
            excluded.append({**event, "excludedBy": reason})
        elif event["scanCount"] >= args.min_scan_count:
            kept.append(event)
    print(f"exclusions: {len(excluded)} events removed ({len(exclusions)} rules); {len(kept)} remain", flush=True)

    # Time-contiguous ordering and block sharding: neighbouring events share
    # hourly DSRF files, per-5-min ACMC files, and S3 hour listings, so a shard
    # that walks scan time in order pays the download cost once per hour
    # instead of once per event.
    kept.sort(key=lambda event: event["bestScan"]["observedAt"])
    if args.shard_count > 1:
        block = (len(kept) + args.shard_count - 1) // args.shard_count
        kept = kept[args.shard_index * block:(args.shard_index + 1) * block]
    if args.limit_events:
        kept = kept[:args.limit_events]

    # Historical prefixes never gain new objects, so listing results are
    # immutable — cache them for the life of the process (replay-only patch;
    # production behaviour is untouched).
    goes_sample.list_s3_prefix = functools.lru_cache(maxsize=8192)(goes_sample.list_s3_prefix)

    session = requests.Session()
    events_path = output_dir / f"events{shard_suffix}.jsonl"
    done = set()
    for existing in output_dir.glob("events*.jsonl"):
        for line in existing.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["eventId"])
            except (ValueError, KeyError):
                continue
    replayed = []
    for number, event in enumerate(kept, 1):
        if event["eventId"] in done:
            continue
        started = time.time()
        try:
            result = replay_event(event, cache_dir, session)
        except Exception as error:
            result = {**event, "disposition": "replay_error", "error": str(error)[:300]}
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")
        replayed.append(result)
        # NetCDF datasets stay open in the module cache; without this trim an
        # hours-long shard grows without bound.
        if len(goes_sample._DATASET_CACHE) > 40:
            for key, dataset in list(goes_sample._DATASET_CACHE.items()):
                try:
                    dataset.close()
                except Exception:
                    pass
                goes_sample._DATASET_CACHE.pop(key, None)
        print(f"[{number}/{len(kept)}] {result['eventId']} scans={result['scanCount']} "
              f"disposition={result.get('disposition')} ({time.time() - started:.1f}s)", flush=True)

    all_results = []
    seen_ids = set()
    for existing in sorted(output_dir.glob("events*.jsonl")):
        for line in existing.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("eventId") not in seen_ids:
                seen_ids.add(event.get("eventId"))
                all_results.append(event)

    queue = [event for event in all_results if event.get("disposition") in ("go", "possible", "near_miss")]
    tier_rank = {"go": 0, "possible": 1, "near_miss": 2}
    queue.sort(key=lambda event: (tier_rank[event["disposition"]], -event["scanCount"],
                                  -(event["bestScan"].get("radarScore") or 0)))
    counts: dict[str, int] = {}
    for event in all_results:
        counts[event.get("disposition", "unknown")] = counts.get(event.get("disposition", "unknown"), 0) + 1

    (output_dir / "excluded.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in excluded), encoding="utf-8")
    (output_dir / "queue.json").write_text(json.dumps(queue, indent=1) + "\n", encoding="utf-8")
    manifest = {
        "schemaVersion": SCHEMA_VERSION, "generatedAt": iso(datetime.now(timezone.utc)),
        "ruleVersion": RULE_VERSION, "modelMethodVersion": MODEL_METHOD_VERSION,
        "fixedLatencySeconds": FIXED_LATENCY_SECONDS, "eventGapMinutes": EVENT_GAP_MINUTES,
        "constants": {"DIRECT_GO_MIN": DIRECT_GO_MIN, "DIRECT_WATCH_MIN": DIRECT_WATCH_MIN,
                      "OPTIMAL_SUN_MIN": OPTIMAL_SUN_MIN, "OPTIMAL_SUN_MAX": OPTIMAL_SUN_MAX,
                      "WATCH_SUN_MIN": WATCH_SUN_MIN, "WATCH_SUN_MAX": WATCH_SUN_MAX,
                      "NEAR_MISS_DNI_MIN": NEAR_MISS_DNI_MIN},
        "poolSeedScans": len(rows), "events": len(events), "excluded": len(excluded),
        "dispositionCounts": counts, "queueLength": len(queue),
        "note": "queue ordering depends on the approximate Open-Meteo historical model lane; "
                "satellite lane is exact archived GOES",
    }
    (output_dir / "queue-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dispositions": counts, "queue": len(queue),
                      "output": str(output_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
