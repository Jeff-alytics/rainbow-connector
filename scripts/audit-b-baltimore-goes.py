#!/usr/bin/env python
"""Sample GOES sunlight evidence at Baltimore's corrected bow time."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "validation" / "audit-b"
REPLAY = AUDIT_DIR / "baltimore-mrms-replay-v1.json"
FIXTURE = AUDIT_DIR / "regression-fixture-v1.json"
OUTPUT = AUDIT_DIR / "baltimore-goes-corrected-window-v1.json"
CACHE = ROOT / ".worker-cache" / "audit-b-goes"

module_path = ROOT / "scripts" / "audit-b-historical-goes.py"
spec = importlib.util.spec_from_file_location("audit_b_historical_goes", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load {module_path}")
historical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(historical)


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def canonical_hash(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def point_samples(name: str, lat: float, lon: float, target: datetime) -> dict:
    return {
        "name": name,
        "lat": lat,
        "lon": lon,
        "goesDsrf": historical.sample_historical(lat, lon, target, historical.DEFAULT_PRODUCT, CACHE),
        "goesAcmc": historical.sample_historical(lat, lon, target, historical.FALLBACK_PRODUCT, CACHE),
    }


def main() -> int:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    by_time = {scan["observedAt"]: scan for scan in replay["scans"]}
    rows = []
    for stamp in ("2026-07-28T23:30:00Z", "2026-07-28T23:40:00Z"):
        scan = by_time[stamp]
        target = parse_utc(stamp)
        nearest = scan["strides"]["10"]["nearest"]
        points = [
            point_samples("Baltimore observer reference", replay["places"]["Baltimore"]["lat"], replay["places"]["Baltimore"]["lon"], target),
            point_samples("Dundalk observer reference", replay["places"]["Dundalk"]["lat"], replay["places"]["Dundalk"]["lon"], target),
        ]
        for place in ("Baltimore", "Dundalk"):
            seed = nearest[place]
            points.append(point_samples(f"nearest production-stride seed to {place}", seed["observerLat"], seed["observerLon"], target))
        rows.append({"targetAt": stamp, "points": points})
    payload = {
        "schemaVersion": 1,
        "purpose": "Corrected-time GOES evidence for the observer-reported Baltimore/Dundalk bow window.",
        "fixtureContentSha256": fixture["contentSha256"],
        "mrmsReplayContentSha256": hashlib.sha256(REPLAY.read_bytes()).hexdigest(),
        "observationWindow": replay["observationWindow"],
        "samples": rows,
    }
    payload["contentSha256"] = canonical_hash(payload)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "samples": len(rows), "sha256": payload["contentSha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
