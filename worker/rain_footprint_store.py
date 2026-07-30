"""Private S3 persistence for MRMS rain-footprint sidecars."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from rain_footprint import SCHEMA_VERSION, attach_record_rain_spans, build_sidecar


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def object_key(sidecar: dict) -> str:
    observed = parse_utc(sidecar["observedAt"])
    return f"rain-footprint/rolling/{SCHEMA_VERSION}/{observed:%Y/%m/%d}/{sidecar['rainFootprintId']}.json.gz"


def encoded(sidecar: dict) -> tuple[bytes, str]:
    raw = (json.dumps(sidecar, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    return gzip.compress(raw, mtime=0), hashlib.sha256(raw).hexdigest()


def persist_rain_footprint(sidecar: dict | None, bucket: str | None = None, s3_client: Any | None = None) -> dict:
    if not sidecar:
        return {"ok": True, "skipped": True, "reason": "rain footprint not generated"}
    bucket = (bucket or os.environ.get("RAINBOW_RESEARCH_BUCKET") or "").strip()
    if not bucket:
        return {"ok": True, "skipped": True, "reason": "research bucket not configured"}
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3")
    body, content_hash = encoded(sidecar)
    key = object_key(sidecar)
    s3_client.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/json",
        ContentEncoding="gzip", ServerSideEncryption="AES256",
        Metadata={"uncompressed-sha256": content_hash, "schema-version": SCHEMA_VERSION, "rain-footprint-id": sidecar["rainFootprintId"]},
    )
    return {"ok": True, "bucket": bucket, "key": key, "rainFootprintId": sidecar["rainFootprintId"], "contentSha256": content_hash, "runs": len(sidecar.get("runs") or []), "bytes": len(body)}


def safe_persist_rain_footprint(sidecar: dict | None, **kwargs) -> dict:
    try:
        return persist_rain_footprint(sidecar, **kwargs)
    except Exception as error:
        print(f"[rain-footprint] write failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300]}


def safe_build_and_persist_rain_footprint(context: dict | None, decision_records: list[dict] | None = None, **kwargs) -> dict:
    """Build and write after live publication; contain both encoding and S3 failures."""
    if not context:
        return {"ok": True, "skipped": True, "reason": "rain footprint context not generated"}
    try:
        attach_record_rain_spans(
            decision_records or [], context.get("candidates") or [],
            context["latitudes"], context["longitudes"], context["rates"],
            context.get("radarObservedAt"),
        )
        sidecar = build_sidecar(
            context["latitudes"], context["longitudes"], context["rates"],
            context["observedAt"], context["sourceKey"],
        )
        return persist_rain_footprint(sidecar, **kwargs)
    except Exception as error:
        print(f"[rain-footprint] build/write failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300]}
