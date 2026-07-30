"""Private rolling S3 storage for Opportunity Ledger shadow records."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from opportunity_ledger import METHOD_VERSION, SCHEMA_VERSION

MAX_PREVIOUS_AGE_MINUTES = 15


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def object_key(ledger: dict) -> str:
    observed = parse_utc(ledger["scanTime"])
    return (
        f"opportunity-ledger/rolling/{SCHEMA_VERSION}/{METHOD_VERSION}/{observed:%Y/%m/%d}/"
        f"opportunity-ledger-{observed:%Y%m%dT%H%M%SZ}.json.gz"
    )


def encode(ledger: dict) -> tuple[bytes, str, int]:
    raw = (json.dumps(ledger, separators=(",", ":"), sort_keys=True) + chr(10)).encode()
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest(), len(raw)


def persist(ledger: dict, bucket: str, s3_client: Any) -> dict:
    body, content_hash, raw_bytes = encode(ledger)
    key = object_key(ledger)
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/json",
        ContentEncoding="gzip", ServerSideEncryption="AES256",
        Metadata={
            "uncompressed-sha256": content_hash,
            "schema-version": SCHEMA_VERSION,
            "method-version": METHOD_VERSION,
            "rain-footprint-id": ledger["rainFootprintId"],
        },
    )
    return {"bucket": bucket, "key": key, "contentSha256": content_hash, "gzipBytes": len(body), "rawBytes": raw_bytes}


def _keys_for_day(bucket: str, day: datetime, s3_client: Any) -> list[str]:
    prefix = f"opportunity-ledger/rolling/{SCHEMA_VERSION}/{METHOD_VERSION}/{day:%Y/%m/%d}/"
    keys, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = s3_client.list_objects_v2(**kwargs)
        keys.extend(item["Key"] for item in response.get("Contents") or [])
        if not response.get("IsTruncated"):
            return keys
        token = response["NextContinuationToken"]


def load_previous(bucket: str, scan_time: str, s3_client: Any,
                  maximum_age_minutes: float = MAX_PREVIOUS_AGE_MINUTES) -> tuple[dict | None, str | None]:
    observed = parse_utc(scan_time)
    candidates = []
    for day in (observed - timedelta(days=1), observed):
        candidates.extend(_keys_for_day(bucket, day, s3_client))
    current_name = f"opportunity-ledger-{observed:%Y%m%dT%H%M%SZ}.json.gz"
    earlier = sorted(key for key in candidates if key.rsplit("/", 1)[-1] < current_name)
    if not earlier:
        return None, None
    key = earlier[-1]
    body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
    previous = json.loads(gzip.decompress(body))
    age_minutes = (observed - parse_utc(previous["scanTime"])).total_seconds() / 60
    if age_minutes <= 0 or age_minutes > maximum_age_minutes:
        return None, None
    return previous, key
