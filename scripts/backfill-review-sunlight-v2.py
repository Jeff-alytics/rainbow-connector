"""Backfill stored sunlight-v2 assessments into existing Review events.

The script reads already-enriched decision logs from the private research
bucket. It never recomputes sunlight and never changes detector disposition.
By default it is a dry run; --apply posts through the normal signed,
idempotent Review callback.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from review_callback import build_payload, callback_secret, payload_batches  # noqa: E402


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def prefixes(start: datetime, end: datetime) -> list[str]:
    day = start.date()
    output = []
    while day <= end.date():
        output.append(
            f"decision-log/rolling/candidate-decision-log.v1/"
            f"{day.year:04d}/{day.month:02d}/{day.day:02d}/"
        )
        day += timedelta(days=1)
    return output


def review_candidate_ids(
    base_url: str, password: str, start: datetime, end: datetime,
) -> tuple[set[str], int]:
    session = requests.Session()
    response = session.post(
        f"{base_url}/api/review-session",
        json={"password": password},
        timeout=30,
    )
    response.raise_for_status()
    results_response = session.get(
        f"{base_url}/api/go-events?results=1&limit=1000", timeout=30,
    )
    results_response.raise_for_status()
    reviewed_event_ids = set()
    reviewed_rows = 0
    for row in results_response.json().get("items") or []:
        reviewed_at = row.get("reviewedAt")
        if not reviewed_at or not start <= instant(reviewed_at) <= end:
            continue
        reviewed_rows += 1
        reviewed_event_ids.add(str(row.get("id") or "").split("::", 1)[0])

    events_response = session.get(f"{base_url}/api/go-events?limit=1000", timeout=30)
    events_response.raise_for_status()
    ids = set()
    for event in events_response.json().get("items") or []:
        if str(event.get("id") or "") not in reviewed_event_ids:
            continue
        for detection in event.get("detections") or []:
            if detection.get("candidateId"):
                ids.add(str(detection["candidateId"]))
    return ids, reviewed_rows


def stored_envelopes(s3, bucket: str, start: datetime, end: datetime):
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in prefixes(start, end):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = item["Key"]
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                envelope = json.loads(gzip.decompress(body))
                observed = instant((envelope.get("radar") or {}).get("observedAt"))
                if start <= observed <= end:
                    yield key, envelope


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--start", required=True, help="Inclusive UTC ISO timestamp")
    parser.add_argument("--end", required=True, help="Inclusive UTC ISO timestamp")
    parser.add_argument("--base-url", default="https://therainbowconnector.com")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    start, end = instant(args.start), instant(args.end)
    password = str(os.environ.get("GO_REVIEW_PASSWORD") or "").strip()
    if not password:
        raise SystemExit("GO_REVIEW_PASSWORD is required")
    target_ids, reviewed_rows = review_candidate_ids(
        args.base_url.rstrip("/"), password, start, end,
    )
    s3 = boto3.client("s3", region_name="us-east-1")
    assessments = {}
    scanned_objects = selected_records = 0
    for _, envelope in stored_envelopes(s3, args.bucket, start, end):
        scanned_objects += 1
        filtered = {
            **envelope,
            "records": [
                record for record in (envelope.get("records") or [])
                if str(record.get("candidateId") or "") in target_ids
            ],
        }
        payload = build_payload(filtered)
        selected_records += len(payload["assessments"])
        for assessment in payload["assessments"]:
            assessments[assessment["idempotencyKey"]] = assessment

    summary = {
        "apply": args.apply,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "reviewedRows": reviewed_rows,
        "reviewCandidateIds": len(target_ids),
        "decisionObjects": scanned_objects,
        "selectedRecords": selected_records,
        "uniqueAssessments": len(assessments),
    }
    if not args.apply:
        print(json.dumps(summary, sort_keys=True))
        return 0

    secret = callback_secret()
    if not secret:
        raise SystemExit("Review enrichment secret is unavailable")
    callback_url = f"{args.base_url.rstrip('/')}/api/review-assessment"
    combined = {
        "schemaVersion": "review-assessment.v1",
        "detectorRuleVersion": "stored-decision-log-backfill",
        "sunlightMethodVersion": "sunlight-v2-shadow-2026-07-v3",
        "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "assessments": list(assessments.values()),
    }
    responses = []
    for _, body in payload_batches(combined):
        response = requests.post(
            callback_url,
            data=body,
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
                "User-Agent": "RainbowConnectorBackfill/1.0",
            },
            timeout=30,
        )
        response.raise_for_status()
        responses.append(response.json())
    for name in ("received", "attached", "duplicates", "unmatched"):
        summary[name] = sum(int(item.get(name) or 0) for item in responses)
    summary["batches"] = len(responses)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
