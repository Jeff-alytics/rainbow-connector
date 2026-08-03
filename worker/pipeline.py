"""End-to-end NOAA candidate pipeline and artifact publisher."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import requests

from build_noaa_candidates import build as build_radar_shortlist
from enrichment import USER_AGENT, enrich_shortlist


def build_final_artifact(
    cache_dir: Path,
    maximum: int = 250,
    stride: int = 10,
    decision_records: list[dict] | None = None,
    research_artifacts: dict | None = None,
) -> dict:
    started = time.monotonic()
    radar = build_radar_shortlist(SimpleNamespace(
        cache_dir=str(cache_dir / "mrms"),
        output=None,
        maximum=maximum,
        stride=stride,
        metadata_only=False,
    ), research_artifacts=research_artifacts)
    artifact = enrich_shortlist(radar, cache_dir / "goes", decision_records=decision_records)
    artifact["runtimeMs"] = round((time.monotonic() - started) * 1000)
    return artifact


def publish_artifact(
    artifact: dict,
    url: str | None = None,
    secret: str | None = None,
    session: requests.Session | None = None,
) -> dict:
    url = (url or os.environ.get("RAINBOW_PUBLISH_URL") or "").strip()
    secret = secret or os.environ.get("SATELLITE_PUBLISH_SECRET") or ""
    if not url:
        raise RuntimeError("RAINBOW_PUBLISH_URL is not configured")
    if not secret:
        raise RuntimeError("SATELLITE_PUBLISH_SECRET is not configured")
    session = session or requests.Session()
    response = session.post(
        url,
        json=artifact,
        headers={"Authorization": f"Bearer {secret}", "User-Agent": USER_AGENT},
        timeout=30,
    )
    try:
        body = response.json()
    except Exception:
        body = {"body": response.text}
    if not response.ok:
        raise RuntimeError(f"Artifact publish failed ({response.status_code}): {body}")
    return body


def notify_subscribers(
    url: str | None = None,
    secret: str | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Ask Vercel to match subscribers against its finalized artifact.

    This deliberately sends no candidate payload: Vercel must read the artifact
    after applying server-side policy. Alert dispatch requires two linked scans;
    first-scan candidates remain visible on the public map but do not email.
    """
    url = (url or os.environ.get("RAINBOW_NOTIFY_URL") or "").strip()
    secret = secret or os.environ.get("ALERT_NOTIFY_SECRET") or ""
    if not url or not secret:
        return {"ok": True, "skipped": True, "reason": "notifications not configured"}
    session = session or requests.Session()
    response = session.post(
        url,
        json={},
        headers={"Authorization": f"Bearer {secret}", "User-Agent": USER_AGENT},
        timeout=60,
    )
    try:
        body = response.json()
    except Exception:
        body = {"body": response.text}
    if not response.ok:
        raise RuntimeError(f"Subscriber notification failed ({response.status_code}): {body}")
    return body


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete NOAA-first rainbow detector")
    parser.add_argument("--cache-dir", default=".worker-cache")
    parser.add_argument("--maximum", type=int, default=250)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--output")
    parser.add_argument("--publish", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = arguments()
    artifact = build_final_artifact(Path(args.cache_dir), args.maximum, args.stride)
    output = {"artifact": artifact}
    if args.publish:
        output["publish"] = publish_artifact(artifact)
    payload = json.dumps(output, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
