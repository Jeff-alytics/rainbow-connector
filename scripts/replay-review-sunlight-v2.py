"""Create exact-point, causally bounded sunlight-v2 replays for Review rows.

The replay uses each event's observer point and original time, but only source
objects available by the matching production decision envelope's generatedAt.
It writes isolated rolling research envelopes and invokes the existing shadow
worker. Nothing changes public classification, maps, alerts, or email.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from decision_log import SUNLIGHT_V2_METHOD_VERSION, candidate_id  # noqa: E402


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def destination(lat: float, lon: float, bearing: float, distance_km: float) -> tuple[float, float]:
    radius = 6371.0088
    angular = distance_km / radius
    phi1, lam1, theta = map(math.radians, (lat, lon, bearing))
    phi2 = math.asin(math.sin(phi1) * math.cos(angular)
                     + math.cos(phi1) * math.sin(angular) * math.cos(theta))
    lam2 = lam1 + math.atan2(math.sin(theta) * math.sin(angular) * math.cos(phi1),
                             math.cos(angular) - math.sin(phi1) * math.sin(phi2))
    return math.degrees(phi2), (math.degrees(lam2) + 540) % 360 - 180


def review_session(base_url: str, password: str) -> requests.Session:
    session = requests.Session()
    response = session.post(f"{base_url}/api/review-session", json={"password": password}, timeout=30)
    response.raise_for_status()
    return session


def missing_events(session: requests.Session, base_url: str, start: datetime, end: datetime) -> list[dict]:
    results = session.get(f"{base_url}/api/go-events?results=1&limit=1000", timeout=30)
    results.raise_for_status()
    ids = {
        str(row.get("id") or "").split("::", 1)[0]
        for row in results.json().get("items") or []
        if row.get("reviewedAt") and start <= instant(row["reviewedAt"]) <= end
        and row.get("sunlightAssessment") is None
    }
    response = session.get(f"{base_url}/api/go-events?limit=1000", timeout=30)
    response.raise_for_status()
    return [event for event in response.json().get("items") or [] if str(event.get("id")) in ids]


def scan_keys(s3, bucket: str, events: list[dict]) -> list[str]:
    days = set()
    for event in events:
        detected = (event.get("representative") or {}).get("detectedAt") or event.get("firstSeenAt")
        moment = instant(detected)
        days.add(moment.strftime("%Y/%m/%d"))
        days.add((moment.replace(hour=0, minute=0, second=0, microsecond=0)).strftime("%Y/%m/%d"))
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for day in days:
        prefix = f"decision-log/rolling/candidate-decision-log.v1/{day}/"
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents") or [])
    return keys


def load_source_envelopes(s3, bucket: str, keys: list[str], events: list[dict]) -> list[tuple[str, dict]]:
    event_times = [instant((event.get("representative") or {}).get("detectedAt") or event["firstSeenAt"])
                   for event in events]
    selected = []
    for key in keys:
        name = key.rsplit("/", 1)[-1]
        try:
            scan = datetime.strptime(name[5:21], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if any(abs((scan - moment).total_seconds()) <= 12 * 60 for moment in event_times):
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            selected.append((key, json.loads(gzip.decompress(body))))
    return selected


def matching_source(event: dict, sources: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    representative = event.get("representative") or {}
    target = instant(representative.get("detectedAt") or event.get("firstSeenAt"))
    historical = str(event.get("candidateType") or "").startswith("historical_")
    ranked = []
    for key, envelope in sources:
        value = (envelope.get("radar") or {}).get("observedAt") if historical else envelope.get("generatedAt")
        if value:
            ranked.append((abs((instant(value) - target).total_seconds()), key, envelope))
    if not ranked:
        return None
    gap, key, envelope = min(ranked, key=lambda item: item[0])
    return (key, envelope) if gap <= 10 * 60 else None


def replay_record(event: dict, radar_observed_at: str) -> dict | None:
    representative = event.get("representative") or {}
    evidence = representative.get("evidence") or {}
    lat, lon = representative.get("lat"), representative.get("lon")
    elevation = evidence.get("sunElevationDeg")
    anti = (representative.get("direction") or {}).get("bearing")
    if not all(isinstance(value, (int, float)) for value in (lat, lon, elevation, anti)):
        return None
    rain_point = evidence.get("rainPoint") or {}
    rain_lat, rain_lon = rain_point.get("lat"), rain_point.get("lon")
    rain_distance = rain_point.get("distanceKm")
    if not isinstance(rain_lat, (int, float)) or not isinstance(rain_lon, (int, float)):
        rain_distance = 15.0
        rain_lat, rain_lon = destination(float(lat), float(lon), float(anti), rain_distance)
    candidate = {
        "lat": float(lat), "lon": float(lon), "rainLat": float(rain_lat), "rainLon": float(rain_lon),
        "rainDistanceKm": rain_distance, "rainRateMmHr": evidence.get("rainIntensity"),
        "observerRainRateMmHr": evidence.get("observerRainIntensity"),
        "sunElevationDeg": float(elevation), "sunBearingDeg": (float(anti) + 180) % 360,
        "antiSolarBearingDeg": float(anti), "radarScore": representative.get("score"),
    }
    goes = evidence.get("goes") or {}
    return {
        "candidateId": candidate_id(candidate, radar_observed_at),
        "disposition": "selected_possible", "decisionStage": "exact_point_causal_replay",
        "decisionReasons": ["review_exact_point_causal_replay_v1"],
        "features": {
            "observer": {"lat": candidate["lat"], "lon": candidate["lon"]},
            "rain": {"lat": candidate["rainLat"], "lon": candidate["rainLon"],
                     "distanceKm": candidate["rainDistanceKm"], "rateMmHr": candidate["rainRateMmHr"],
                     "observerRateMmHr": candidate["observerRainRateMmHr"]},
            "geometry": {"sunElevationDeg": candidate["sunElevationDeg"],
                         "sunBearingDeg": candidate["sunBearingDeg"],
                         "antiSolarBearingDeg": candidate["antiSolarBearingDeg"],
                         "radarScore": candidate["radarScore"]},
            "model": {"directNormalIrradianceWm2": evidence.get("directNormalIrradianceWm2"),
                      "cloudCoverPct": evidence.get("cloudCoverPct")},
            "satellite": {"decision": goes.get("decision")},
            "persistence": {"priorMatchingScans": 0}, "sunlightV2": None,
            "thresholdSnapshot": {},
        },
        "replayTargetEventId": event.get("id"),
    }


def encoded(value: dict) -> bytes:
    return gzip.compress(json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), mtime=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--base-url", default="https://therainbowconnector.com")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    password = str(os.environ.get("GO_REVIEW_PASSWORD") or "").strip()
    if not password:
        raise SystemExit("GO_REVIEW_PASSWORD is required")
    start, end = instant(args.start), instant(args.end)
    base_url = args.base_url.rstrip("/")
    session = review_session(base_url, password)
    events = missing_events(session, base_url, start, end)
    s3 = boto3.client("s3", region_name="us-east-1")
    sources = load_source_envelopes(s3, args.bucket, scan_keys(s3, args.bucket, events), events)
    groups: dict[str, dict] = {}
    skipped = []
    for event in events:
        source = matching_source(event, sources)
        if not source:
            skipped.append({"eventId": event.get("id"), "reason": "no_causal_source_envelope"})
            continue
        source_key, envelope = source
        radar_observed_at = (envelope.get("radar") or {}).get("observedAt")
        record = replay_record(event, radar_observed_at)
        if not record:
            skipped.append({"eventId": event.get("id"), "reason": "incomplete_event_geometry"})
            continue
        group = groups.setdefault(source_key, {"source": envelope, "records": []})
        group["records"].append(record)
    plan = {"missingEvents": len(events), "replayableEvents": sum(len(g["records"]) for g in groups.values()),
            "sourceScans": len(groups), "skipped": skipped, "apply": args.apply}
    if not args.apply:
        print(json.dumps(plan, sort_keys=True))
        return 0
    lam = boto3.client("lambda", region_name="us-east-1")
    replay_keys = []
    for source_key, group in groups.items():
        source, records = group["source"], group["records"]
        radar = source.get("radar") or {}
        digest = hashlib.sha256("|".join(sorted(record["replayTargetEventId"] for record in records)).encode()).hexdigest()[:12]
        stamp = instant(radar["observedAt"]).strftime("%Y%m%dT%H%M%SZ")
        key = f"decision-log/rolling/review-exact-point-replay.v1/{stamp}-{digest}.json.gz"
        envelope = {
            "schemaVersion": "candidate-decision-log.v1", "generatedAt": source.get("generatedAt"),
            "detectorRuleVersion": "review-exact-point-causal-replay-v1", "radar": radar,
            "records": records, "metrics": {},
            "shadowV2": {"methodVersion": SUNLIGHT_V2_METHOD_VERSION, "status": "pending"},
        }
        s3.put_object(Bucket=args.bucket, Key=key, Body=encoded(envelope), ContentType="application/json",
                      ContentEncoding="gzip", ServerSideEncryption="AES256")
        response = lam.invoke(FunctionName=args.function, InvocationType="Event",
                              Payload=json.dumps({"bucket": args.bucket, "key": key}).encode())
        if response.get("StatusCode") != 202:
            raise RuntimeError(f"Replay invocation was not accepted for {key}")
        replay_keys.append(key)
    plan["queued"] = len(replay_keys)
    plan["replayKeys"] = replay_keys
    print(json.dumps(plan, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
