#!/usr/bin/env python
"""V2 sunward-gap replay over the labeled study subset.

Runs production `enrich_sunlight_v2` (worker/sunlight_v2.py, with the verified
historical shims: archived GOES via causal cutoffs, injected IEM ASOS METARs,
pinned assessment time) against a stratified subset of human-labeled events:
every confirmed rainbow, every GO event, and persistence-matched no-rainbow
controls. Output: v2-replay.jsonl — one record per event with the full
sunlightV2 feature block. Resumable; no production store access.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "worker", ROOT / "api", ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from build_historical_review_queue import fetch_historical_model  # noqa: E402
from decision_log import build_record  # noqa: E402
from enrichment import (DIRECT_GO_MIN, DIRECT_WATCH_MIN, OPTIMAL_SUN_MAX,  # noqa: E402
                        OPTIMAL_SUN_MIN)
from replay_historical_sunlight import fetch_archived_asos, station_catalog  # noqa: E402
from sunlight_decision import build_decision, collect_sources  # noqa: E402
from sunlight_v2 import enrich_sunlight_v2, parse_utc  # noqa: E402

QUEUE = ROOT / "validation" / "historical-review-queue"
OUTPUT = QUEUE / "v2-replay.jsonl"
IEM_METAR_METHOD_VERSION = "iem-asos-archive-replay-v1"
FIXED_LATENCY_SECONDS = 8 * 60
CONTROLS_PER_BAND = 20


def iso(value) -> str:
    return value.astimezone().isoformat().replace("+00:00", "Z") if hasattr(value, "astimezone") else value


def load_events() -> dict[str, dict]:
    events = {}
    for path in QUEUE.glob("events*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            events[event["eventId"]] = event
    return events


def load_labels() -> tuple[set[str], set[str]]:
    r1 = json.loads((QUEUE / "human-labels-round1-2026-08-02.json").read_text(encoding="utf-8"))
    r3 = json.loads((QUEUE / "human-labels-round3-2026-08-02.json").read_text(encoding="utf-8"))
    corrections = json.loads((QUEUE / "human-labels-corrections-2026-08-02.json").read_text(encoding="utf-8"))
    rainbows = {label["file"].split("/")[1] for label in r1["labels"] if label["label"] == "rainbow"}
    rainbows |= {entry["eventId"] for entry in r3.get("eventLabels", []) if entry["label"] == "rainbow"}
    rainbows |= {entry["eventId"] for entry in corrections["events"]}
    unusable = {entry["eventId"] for entry in r3.get("eventLabels", []) if entry["label"] == "unusable"}
    return rainbows, unusable


def select_subset(events: dict[str, dict], rainbows: set[str], unusable: set[str]) -> list[dict]:
    reviewed_rounds = json.loads((QUEUE / "reviewed-rounds.json").read_text(encoding="utf-8"))
    reviewed = {eid for entry in reviewed_rounds["rounds"] for eid in entry["eventIds"]}
    chosen: dict[str, str] = {}
    for eid in rainbows & reviewed:
        chosen[eid] = "rainbow"
    for eid, event in events.items():
        if event.get("disposition") == "go" and eid in reviewed and eid not in chosen:
            chosen[eid] = "go_no_rainbow"
    # persistence-matched controls: top radar no-rainbow events per scan band
    negatives = [event for eid, event in events.items()
                 if eid in reviewed and eid not in chosen and eid not in unusable
                 and event.get("disposition") in ("possible", "near_miss")]
    for low, high in ((13, 999), (5, 12), (1, 4)):
        band = sorted((event for event in negatives if low <= event["scanCount"] <= high),
                      key=lambda event: -(event["bestScan"].get("radarScore") or 0))
        for event in band[:CONTROLS_PER_BAND]:
            chosen[event["eventId"]] = f"control_{low}plus" if high > 100 else f"control_{low}_{high}"
    subset = [{**events[eid], "studyRole": role} for eid, role in chosen.items()]
    subset.sort(key=lambda event: event["bestScan"]["observedAt"])
    return subset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-events", type=int, default=0)
    args = parser.parse_args()

    events = load_events()
    rainbows, unusable = load_labels()
    subset = select_subset(events, rainbows, unusable)
    if args.limit_events:
        subset = subset[:args.limit_events]
    done = set()
    if OUTPUT.exists():
        for line in OUTPUT.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            message = str(row.get("error") or (row.get("v2") or {}).get("error") or "")
            # transient network failures get retried on resume; genuine data
            # gaps (e.g. the July 5-6 DSRF outage) stay recorded
            if any(token in message for token in ("Connection", "Remote", "timed out", "timeout")):
                continue
            done.add(row["eventId"])
    pending = [event for event in subset if event["eventId"] not in done]
    roles = {}
    for event in subset:
        roles[event["studyRole"]] = roles.get(event["studyRole"], 0) + 1
    print(f"subset {len(subset)} events {roles}; {len(pending)} pending", flush=True)

    session = requests.Session()
    # archived METARs: one IEM fetch per UTC day, only stations within 80 km of a
    # subset event (nearest_metars uses a 30 km radius, so 80 km is generous).
    import math
    catalog = station_catalog(ROOT / "validation" / "faa" / "sites.json")
    def near_subset(site) -> bool:
        for event in subset:
            scan = event["bestScan"]
            dlat = (float(site["latitude"]) - scan["lat"]) * 111
            dlon = (float(site["longitude"]) - scan["lon"]) * 111 * math.cos(math.radians(scan["lat"]))
            if dlat * dlat + dlon * dlon <= 80 * 80:
                return True
        return False
    catalog = {key: site for key, site in catalog.items() if near_subset(site)}
    print(f"METAR station catalog trimmed to {len(catalog)} stations", flush=True)

    def fetch_day_metars(day_start):
        for attempt in range(4):
            try:
                return fetch_archived_asos(catalog, day_start, day_start + timedelta(days=1), session)
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code == 429 and attempt < 3:
                    time.sleep(30 * (attempt + 1))
                    continue
                raise
        return []
    metars_by_day: dict[str, list[dict]] = {}

    cache_dir = QUEUE / "v2-cache"
    constants = {"direct_watch_min": DIRECT_WATCH_MIN, "direct_go_min": DIRECT_GO_MIN,
                 "fallback_radar_min": 90, "fallback_rain_distance_max": 15,
                 "fallback_observer_rain_max": 0.05,
                 "optimal_sun_min": OPTIMAL_SUN_MIN, "optimal_sun_max": OPTIMAL_SUN_MAX}

    for number, event in enumerate(pending, 1):
        started = time.time()
        scan = event["bestScan"]
        observed_at = scan["observedAt"]
        available_by = (parse_utc(observed_at) + timedelta(seconds=FIXED_LATENCY_SECONDS)
                        ).isoformat().replace("+00:00", "Z")
        day = observed_at[:10]
        if day not in metars_by_day:
            try:
                metars_by_day[day] = fetch_day_metars(parse_utc(day + "T00:00:00Z"))
            except Exception as error:
                print(f"  METAR fetch failed for {day}: {str(error)[:120]}", flush=True)
                metars_by_day[day] = []
        try:
            candidate = {key: scan[key] for key in
                         ("lat", "lon", "rainLat", "rainLon", "rainDistanceKm", "rainRateMmHr",
                          "observerRainRateMmHr", "sunElevationDeg", "sunBearingDeg",
                          "antiSolarBearingDeg", "radarScore")}
            sources = collect_sources(scan["lat"], scan["lon"], "auto", 3, cache_dir / "v1-goes", False,
                                      observed_at=observed_at, available_by=available_by)
            decision = build_decision(sources)
            record = build_record(candidate, observed_at, "historical_replay", "historical_replay",
                                  ["v2_labeled_subset"], constants, decision, sources)
            try:
                record["features"]["model"] = fetch_historical_model(
                    scan["lat"], scan["lon"], observed_at, session)
            except Exception:
                pass
            result = enrich_sunlight_v2([record], [candidate], observed_at, cache_dir / "v2",
                                        session=session, assessment_processing_at=available_by,
                                        metars=metars_by_day[day],
                                        metar_method_version=IEM_METAR_METHOD_VERSION)
            v2_error = str((record["features"].get("sunlightV2") or {}).get("error") or "")
            if any(token in v2_error for token in ("Connection", "Remote", "timed out", "timeout")):
                time.sleep(10)
                result = enrich_sunlight_v2([record], [candidate], observed_at, cache_dir / "v2",
                                            session=session, assessment_processing_at=available_by,
                                            metars=metars_by_day[day],
                                            metar_method_version=IEM_METAR_METHOD_VERSION)
            row = {"eventId": event["eventId"], "studyRole": event["studyRole"],
                   "disposition": event.get("disposition"), "scanCount": event["scanCount"],
                   "observedAt": observed_at, "assessmentProcessingAt": available_by,
                   "v2": record["features"].get("sunlightV2"),
                   "v2Run": {key: result.get(key) for key in ("ok", "enriched", "errors")}}
        except Exception as error:
            row = {"eventId": event["eventId"], "studyRole": event["studyRole"],
                   "error": str(error)[:300]}
        with OUTPUT.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        state = (row.get("v2") or {}).get("sunlightState", "error")
        print(f"[{number}/{len(pending)}] {event['eventId']} role={event['studyRole']} "
              f"v2={state} ({time.time() - started:.0f}s)", flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
