"""Private S3 persistence for MRMS rain-footprint sidecars."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from detector_core import observer_seeds_from_rain_grid
from rain_footprint import SCHEMA_VERSION, attach_candidate_rain_spans, attach_record_rain_spans, build_sidecar

CANDIDATE_SEED_CONTRACT = "camera-independent-decision-seeds.v2"
V5_EXPANDED_SEED_CONTRACT = "v5-expanded-camera-independent-seeds.v1"


def candidate_seeds_from_records(records: list[dict] | None) -> list[dict]:
    """Retain the complete pre-camera detector pool needed by frozen V4."""
    seeds = []
    for record in records or []:
        features = record.get("features") or {}
        observer, rain = features.get("observer") or {}, features.get("rain") or {}
        geometry = features.get("geometry") or {}
        seeds.append({
            "candidateId": record.get("candidateId"),
            "lat": observer.get("lat"), "lon": observer.get("lon"),
            "rainLat": rain.get("lat"), "rainLon": rain.get("lon"),
            "rainDistanceKm": rain.get("distanceKm"), "rainRateMmHr": rain.get("rateMmHr"),
            "observerRainRateMmHr": rain.get("observerRateMmHr"),
            "spatialSupport": rain.get("spatialSupport"),
            "antiSolarRainArcSpanDeg": rain.get("antiSolarRainArcSpanDeg"),
            "antiSolarRainSpanByTier": rain.get("antiSolarRainSpanByTier"),
            "sunElevationDeg": geometry.get("sunElevationDeg"),
            "sunBearingDeg": geometry.get("sunBearingDeg"),
            "antiSolarBearingDeg": geometry.get("antiSolarBearingDeg"),
            "radarScore": geometry.get("radarScore"),
        })
    return seeds


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
        sidecar["candidateSeedContract"] = CANDIDATE_SEED_CONTRACT
        sidecar["candidateSeedSource"] = "retained_cap_and_stride_limited_camera_independent_detector_pool"
        sidecar["candidateSeeds"] = candidate_seeds_from_records(decision_records)
        expanded_diagnostics = {}
        operational = context.get("operationalSeedParameters") or {}
        expanded_seeds = observer_seeds_from_rain_grid(
            context["latitudes"], context["longitudes"], context["rates"], context["observedAt"],
            stride=max(1, int(operational.get("stride") or 10)),
            maximum=None,
            diagnostics=expanded_diagnostics,
            enforce_spatial_support=False,
            sun_elevation_range=(-2.0, 42.0),
            require_dry_observer=False,
            retain_all_observer_distances=True,
            cluster_radius_km=0.0,
        )
        attach_candidate_rain_spans(
            expanded_seeds, context["latitudes"], context["longitudes"], context["rates"],
        )
        sidecar["v5ExpandedSeedContract"] = V5_EXPANDED_SEED_CONTRACT
        sidecar["v5ExpandedSeeds"] = expanded_seeds
        sidecar["upstreamSeedAudit"] = {
            "schemaVersion": "v5-upstream-seed-audit.v1",
            "operational": context.get("operationalSeedDiagnostics") or {},
            "expanded": expanded_diagnostics,
            "operationalRetainedSeeds": len(sidecar["candidateSeeds"]),
            "expandedRetainedSeeds": len(expanded_seeds),
            "expandedPoolDescription": "stride-limited, unclustered, uncapped; solar -2 to 42 degrees; observer wetness and isolation retained",
        }
        return persist_rain_footprint(sidecar, **kwargs)
    except Exception as error:
        print(f"[rain-footprint] build/write failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300]}
