"""One compact, scan-level V5 projection to the private Review site."""

from __future__ import annotations

import json
import os

import requests

from review_callback import callback_secret

MAXIMUM_BYTES = 7 * 512 * 1024


def chunk_v5_review_feed(feed: dict, maximum_bytes: int = MAXIMUM_BYTES) -> list[dict]:
    """Split only oversized projections without dropping any family or geometry."""
    raw = json.dumps(feed, separators=(",", ":")).encode("utf-8")
    if len(raw) <= maximum_bytes:
        return [feed]
    base = {key: value for key, value in feed.items() if key != "items"}
    source_hash = str(feed.get("predictionManifestSha256") or feed.get("scanTime") or "v5-scan")
    overhead = len(json.dumps({**base, "items": [], "feedChunk": {"index": 9999, "total": 9999}},
                              separators=(",", ":")).encode("utf-8")) + 256
    batches, batch, batch_bytes = [], [], overhead
    for item in feed.get("items") or []:
        item_bytes = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if item_bytes + overhead > maximum_bytes:
            raise ValueError("A single V5 family exceeds the review feed request limit")
        if batch and batch_bytes + item_bytes > maximum_bytes:
            batches.append(batch)
            batch, batch_bytes = [], overhead
        batch.append(item)
        batch_bytes += item_bytes
    if batch or not batches:
        batches.append(batch)
    chunks = []
    for index, items in enumerate(batches, 1):
        chunk = {**base, "items": items, "sourcePredictionManifestSha256": source_hash,
                 "predictionManifestSha256": f"{source_hash}:chunk:{index}/{len(batches)}",
                 "feedChunk": {"index": index, "total": len(batches)}}
        if len(json.dumps(chunk, separators=(",", ":")).encode("utf-8")) > maximum_bytes:
            raise ValueError("V5 review feed chunk sizing failed")
        chunks.append(chunk)
    return chunks


def push_v5_review_feed(feed: dict, session=None, secret: str | None = None) -> dict:
    url = str(os.environ.get("RAINBOW_REVIEW_ENRICH_URL") or "").strip()
    if not url:
        return {"ok": True, "skipped": True, "reason": "review callback not configured", "items": len(feed.get("items") or [])}
    secret = secret if secret is not None else callback_secret()
    if not secret:
        return {"ok": True, "skipped": True, "reason": "review callback not configured", "items": len(feed.get("items") or [])}
    client, total_bytes, results = session or requests.Session(), 0, []
    chunks = chunk_v5_review_feed(feed)
    for chunk in chunks:
        body = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
        total_bytes += len(body)
        response = client.post(
            url, data=body,
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json",
                     "User-Agent": "RainbowConnector/1.0"},
            timeout=30,
        )
        try:
            result = response.json()
        except Exception:
            result = {"body": response.text[:300]}
        if not response.ok:
            raise RuntimeError(f"V5 Review feed failed ({response.status_code}): {result}")
        results.append(result)
    return {"ok": True, "items": len(feed.get("items") or []), "bytes": total_bytes,
            "chunks": len(chunks), "responses": results}


def safe_push_v5_review_feed(feed: dict) -> dict:
    try:
        return push_v5_review_feed(feed)
    except Exception as error:
        print(f"[v5-review-feed] push failed: {str(error)[:300]}")
        return {"ok": False, "operationalImpact": False, "error": str(error)[:300],
                "items": len(feed.get("items") or [])}
