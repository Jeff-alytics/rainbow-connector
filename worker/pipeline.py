"""End-to-end NOAA candidate pipeline and artifact publisher."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    after applying server-side policy. A fresh strict GO may alert immediately;
    persistence remains useful supporting evidence and duplicate suppression.
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


def capture_dot_evidence(
    url: str | None = None,
    secret: str | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Capture current frames for pending DOT-camera review jobs."""
    url = (url or os.environ.get("RAINBOW_DOT_EVIDENCE_URL") or "").strip()
    secret = secret or os.environ.get("ALERT_NOTIFY_SECRET") or ""
    if not url or not secret:
        return {"ok": True, "skipped": True, "reason": "DOT evidence not configured"}
    session = session or requests.Session()
    import imageio_ffmpeg
    headers = {"Authorization": f"Bearer {secret}", "User-Agent": USER_AGENT}
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    jobs = (response.json().get("jobs") or [])[:9]
    frames = []
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    for job in jobs:
        image_bytes = None
        try:
            if job.get("imageUrl"):
                try:
                    snapshot = session.get(job["imageUrl"], headers={"User-Agent": USER_AGENT}, timeout=20)
                    snapshot.raise_for_status()
                    if "image/" in snapshot.headers.get("Content-Type", "").lower() and len(snapshot.content) >= 8000:
                        image_bytes = snapshot.content
                except requests.RequestException:
                    pass
            if image_bytes is None and job.get("streamUrl"):
                completed = subprocess.run(
                    [ffmpeg, "-loglevel", "error", "-i", job["streamUrl"],
                     "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True,
                )
                if len(completed.stdout) >= 8000:
                    image_bytes = completed.stdout
            if image_bytes is None:
                continue
            import base64
            frames.append({
                **{key: value for key, value in job.items() if key not in {"streamUrl", "imageUrl"}},
                "observedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "imageBase64": base64.b64encode(image_bytes).decode("ascii"),
            })
        except (KeyError, requests.RequestException, subprocess.SubprocessError):
            continue
    if not frames:
        return {"ok": True, "jobs": len(jobs), "stored": 0}
    stored = session.post(url, json={"frames": frames}, headers=headers, timeout=45)
    stored.raise_for_status()
    return {**stored.json(), "jobs": len(jobs), "captured": len(frames)}


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
