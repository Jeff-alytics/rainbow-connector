"""Private S3 storage for replayable candidate decision logs."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from decision_log import RULE_VERSION, SUNLIGHT_V2_METHOD_VERSION

SCHEMA_VERSION = "candidate-decision-log.v1"


def disagreement_metrics(records: list[dict]) -> dict:
    dispositions = Counter(record.get("disposition", "unknown") for record in records)
    disagreement = {}
    for disposition in dispositions:
        values = [
            (record.get("features", {}).get("sunlightV2") or {}).get("disagreesWithV1")
            for record in records if record.get("disposition", "unknown") == disposition
        ]
        measured = [value for value in values if isinstance(value, bool)]
        disagreement[disposition] = {
            "records": len(values), "measured": len(measured), "missing": len(values) - len(measured),
            "disagreements": sum(measured), "rate": (sum(measured) / len(measured)) if measured else None,
        }
    return disagreement


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_envelope(artifact: dict, records: list[dict]) -> dict:
    radar = ((artifact.get("sourceHealth") or {}).get("radar") or {})
    observed_at = radar.get("observedAt") or artifact.get("generatedAt")
    observed = parse_utc(observed_at)
    scan_id = "mrms-" + observed.strftime("%Y%m%dT%H%M%SZ")
    dispositions = Counter(record.get("disposition", "unknown") for record in records)
    disagreement = disagreement_metrics(records)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "detectorRuleVersion": RULE_VERSION,
        "scanId": scan_id,
        "generatedAt": artifact.get("generatedAt"),
        "radar": {
            "provider": radar.get("provider"),
            "observedAt": radar.get("observedAt"),
            "sourceKey": radar.get("s3Key"),
            "rainFootprintId": radar.get("rainFootprintId"),
            "rainFootprintContentSha256": radar.get("rainFootprintContentSha256"),
        },
        "retention": {"class": "rolling", "expiresAfterDays": 14, "freezeEligible": True},
        "metrics": {
            "recordsByDisposition": dict(sorted(dispositions.items())),
            "v1V2DisagreementRateByDisposition": disagreement,
        },
        "shadowV2": {"methodVersion": SUNLIGHT_V2_METHOD_VERSION, "status": "pending" if records else "not_applicable"},
        "records": records,
    }


def object_key(envelope: dict) -> str:
    observed = parse_utc(envelope["radar"]["observedAt"] or envelope["generatedAt"])
    return (
        f"decision-log/rolling/{SCHEMA_VERSION}/"
        f"{observed:%Y/%m/%d}/{envelope['scanId']}.json.gz"
    )


def encoded(envelope: dict) -> tuple[bytes, str]:
    raw = (json.dumps(envelope, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest()


def persist_decision_log(
    artifact: dict,
    records: list[dict],
    bucket: str | None = None,
    s3_client: Any | None = None,
) -> dict:
    bucket = (bucket or os.environ.get("RAINBOW_RESEARCH_BUCKET") or "").strip()
    if not bucket:
        return {"ok": True, "skipped": True, "reason": "research bucket not configured"}
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3")
    envelope = build_envelope(artifact, records)
    body, content_hash = encoded(envelope)
    key = object_key(envelope)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ContentEncoding="gzip",
        ServerSideEncryption="AES256",
        Metadata={
            "uncompressed-sha256": content_hash,
            "schema-version": SCHEMA_VERSION,
            "detector-rule-version": RULE_VERSION,
        },
    )
    return {"ok": True, "bucket": bucket, "key": key, "records": len(records), "bytes": len(body)}


def safe_persist_decision_log(artifact: dict, records: list[dict], **kwargs) -> dict:
    try:
        return persist_decision_log(artifact, records, **kwargs)
    except Exception as error:
        print(f"[decision-log] write failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300], "records": len(records)}
