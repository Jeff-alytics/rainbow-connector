from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sunlight_decision import build_decision, collect_sources


USER_AGENT = "RainbowSatelliteCron/0.1"
DEFAULT_CACHE_DIR = Path("/tmp/rainbow-goes-cache")


def json_response(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def same_origin(handler: BaseHTTPRequestHandler) -> str:
    configured = os.environ.get("SATELLITE_CRON_ORIGIN") or os.environ.get("ALERT_BASE_URL")
    if configured:
        return configured.rstrip("/")
    # Vercel Cron invokes the generated deployment hostname, which may be protected
    # by Vercel Authentication. Use the public production domain for internal calls.
    if os.environ.get("VERCEL_ENV") == "production":
        return "https://therainbowconnector.com"
    host = handler.headers.get("x-forwarded-host") or handler.headers.get("host") or "therainbowconnector.com"
    proto = handler.headers.get("x-forwarded-proto") or "https"
    return f"{proto}://{host}"


def query_params(handler: BaseHTTPRequestHandler) -> dict[str, list[str]]:
    return parse_qs(urlparse(handler.path).query)


def first(params: dict[str, list[str]], key: str, fallback: str = "") -> str:
    values = params.get(key) or []
    return values[0] if values else fallback


def authorized(handler: BaseHTTPRequestHandler) -> bool:
    user_agent = handler.headers.get("user-agent", "")
    if user_agent.startswith("vercel-cron/"):
        return True

    expected = (
        os.environ.get("SATELLITE_CRON_SECRET")
        or os.environ.get("CRON_SECRET")
        or os.environ.get("SATELLITE_PUBLISH_SECRET")
        or ""
    )
    if not expected:
        return False
    auth = handler.headers.get("authorization", "")
    token = auth.replace("Bearer ", "", 1) if auth.lower().startswith("bearer ") else ""
    token = token or first(query_params(handler), "secret")
    return bool(token) and token == expected


def compact_source(source: dict) -> dict:
    keys = [
        "available",
        "fresh",
        "positive",
        "negative",
        "strength",
        "reason",
        "observedAt",
        "observedAgeMinutes",
        "maxAgeMinutes",
        "product",
        "variable",
        "satellite",
        "satellitePosition",
        "value",
        "units",
        "cloudCoverPct",
        "directNormalIrradianceWm2",
        "directRadiationWm2",
        "s3Key",
    ]
    return {k: source.get(k) for k in keys if k in source}


def enrich_candidate(candidate: dict, hours_back: int) -> dict:
    sources = collect_sources(
        lat=candidate["lat"],
        lon=candidate["lon"],
        satellite="auto",
        hours_back=hours_back,
        cache_dir=DEFAULT_CACHE_DIR,
        open_meteo=False,
    )
    decision = build_decision(sources)
    enriched = dict(candidate)
    evidence = dict(enriched.get("evidence") or {})
    evidence["satelliteSunlight"] = {
        "decision": decision["sunlightDecision"],
        "sunlit": decision["sunlit"],
        "confidence": decision["confidence"],
        "positiveSources": decision["positiveSources"],
        "negativeSources": decision["negativeSources"],
        "goSources": decision["goSources"],
        "conflictSources": decision["conflictSources"],
        "sources": {name: compact_source(source) for name, source in sources.items()},
    }
    enriched["evidence"] = evidence
    enriched["sunlightDecision"] = decision["sunlightDecision"]
    enriched["sunlightConfidence"] = decision["confidence"]
    enriched["satelliteSunlit"] = decision["sunlit"]
    return enriched


def summarize(candidates: list[dict], errors: list[dict]) -> dict:
    counts: dict[str, int] = {}
    source_yes: dict[str, int] = {}
    source_no: dict[str, int] = {}
    for candidate in candidates:
        sat = (candidate.get("evidence") or {}).get("satelliteSunlight") or {}
        decision = sat.get("decision") or "unknown"
        counts[decision] = counts.get(decision, 0) + 1
        for src in sat.get("positiveSources") or []:
            source_yes[src] = source_yes.get(src, 0) + 1
        for src in sat.get("negativeSources") or []:
            source_no[src] = source_no.get(src, 0) + 1
    return {
        "enrichedCandidates": len(candidates),
        "errors": len(errors),
        "decisions": dict(sorted(counts.items())),
        "positiveSources": dict(sorted(source_yes.items())),
        "negativeSources": dict(sorted(source_no.items())),
    }


def build_satellite_artifact(origin: str, limit: int, hours_back: int) -> dict:
    candidate_url = (
        f"{origin}/api/candidates?skipSatellite=1&maxVerify=250&maxGo={limit}"
        "&gridKm=12&clusterKm=35&batchSize=10"
    )
    detector_secret = (
        os.environ.get("SATELLITE_CRON_SECRET")
        or os.environ.get("CRON_SECRET")
        or os.environ.get("SATELLITE_PUBLISH_SECRET")
        or ""
    )
    if not detector_secret:
        raise RuntimeError("A satellite cron or publish secret is not configured")
    response = requests.get(
        candidate_url,
        timeout=85,
        headers={
            "Authorization": f"Bearer {detector_secret}",
            "User-Agent": USER_AGENT,
        },
    )
    response.raise_for_status()
    artifact = response.json()

    input_candidates = artifact.get("candidates") or []
    candidates_to_process = input_candidates[:limit]
    enriched: list[dict] = []
    errors: list[dict] = []

    for candidate in candidates_to_process:
        try:
            hit = enrich_candidate(candidate, hours_back)
            if hit.get("satelliteSunlit"):
                enriched.append(hit)
        except Exception as err:
            errors.append({
                "id": candidate.get("id"),
                "rank": candidate.get("rank"),
                "label": candidate.get("label"),
                "lat": candidate.get("lat"),
                "lon": candidate.get("lon"),
                "error": str(err),
            })

    completed_at = datetime.now(timezone.utc)
    cadence_minutes = float(artifact.get("cadenceMinutes") or 5)
    expires_at = completed_at + timedelta(minutes=cadence_minutes)

    return {
        **artifact,
        "candidateGeneratedAt": artifact.get("generatedAt"),
        "schemaVersion": 3,
        "dataStatus": "live",
        "generatedAt": completed_at.isoformat().replace("+00:00", "Z"),
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
        "source": {
            **(artifact.get("source") or {}),
            "satelliteSunlight": "GOES DSRF + GOES ACMC hierarchy via Vercel Cron",
        },
        "candidates": enriched,
        "satelliteDiagnostics": {
            "inputCandidates": len(input_candidates),
            "processedCandidates": len(candidates_to_process),
            "includedNonSunlit": False,
            **summarize(enriched, errors),
            "errors": errors,
        },
    }


def publish_artifact(origin: str, artifact: dict) -> dict:
    secret = (
        os.environ.get("SATELLITE_PUBLISH_SECRET")
        or os.environ.get("ALERT_NOTIFY_SECRET")
        or os.environ.get("BLOB_READ_WRITE_TOKEN")
        or ""
    )
    if not secret:
        raise RuntimeError("SATELLITE_PUBLISH_SECRET or BLOB_READ_WRITE_TOKEN is not configured")
    response = requests.post(
        f"{origin}/api/satellite-candidates",
        json=artifact,
        timeout=30,
        headers={
            "Authorization": f"Bearer {secret}",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        body = response.json()
    except Exception:
        body = {"body": response.text}
    if not response.ok:
        raise RuntimeError(f"satellite artifact publish failed ({response.status_code}): {body}")
    return body


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        started = time.time()
        if not authorized(self):
            json_response(self, 401, {"ok": False, "error": "Unauthorized"})
            return

        params = query_params(self)
        limit = max(1, min(100, int(first(params, "limit", os.environ.get("SATELLITE_CRON_LIMIT", "50")))))
        hours_back = max(1, min(12, int(first(params, "hoursBack", "6"))))
        origin = same_origin(self)

        try:
            artifact = build_satellite_artifact(origin, limit, hours_back)
            stored = publish_artifact(origin, artifact)
            json_response(self, 200, {
                "ok": True,
                "runtimeMs": int((time.time() - started) * 1000),
                "generatedAt": artifact.get("generatedAt"),
                "expiresAt": artifact.get("expiresAt"),
                "candidates": len(artifact.get("candidates") or []),
                "satelliteDiagnostics": artifact.get("satelliteDiagnostics"),
                "storage": stored,
            })
        except Exception as err:
            json_response(self, 502, {
                "ok": False,
                "runtimeMs": int((time.time() - started) * 1000),
                "error": str(err),
            })
