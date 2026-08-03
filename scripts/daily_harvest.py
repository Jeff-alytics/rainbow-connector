#!/usr/bin/env python
"""Daily prospective harvest — captures the perishable half of the record.

MRMS is a deep archive; FAA camera frames are not (roughly 30 days rolling). Every
day without a harvest is a day of imagery that can never be recovered, so this job
runs independently of any gate, ranking decision, or deployment.

For one UTC day it:
  1. builds the camera-matched candidate pool from archived MRMS (production seed
     generator + production camera matcher),
  2. groups seeds into site events and keeps a deliberately GENEROUS superset — the
     ranking is not frozen, so the harvest must not presuppose one,
  3. downloads FAA frames for those events with SHA-256 hashes, and
  4. writes an immutable, hash-chained prediction record capturing what the current
     pipeline believed BEFORE any human looked, per the predict-then-review protocol.

Resumable and incremental: re-running a date completes it rather than restarting.
Nothing here touches production, and no ranking is selected or frozen by it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "worker", ROOT / "api", ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from build_historical_review_queue import build_events, load_pool_rows  # noqa: E402

SCHEMA_VERSION = "daily-harvest.v1"
HARVEST_ROOT = ROOT / "validation" / "harvest"
# Superset policy: keep anything with persistence OR strong radar geometry. This is
# intentionally wider than any candidate ranking, because the harvest must still be
# useful after the ranking changes.
MIN_SCAN_COUNT = 2
MIN_RADAR_SCORE = 90.0
MAX_EVENTS_PER_DAY = 250


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run(command: list[str]) -> int:
    print("  $", " ".join(str(part) for part in command[-4:]), flush=True)
    return subprocess.run(command, cwd=ROOT).returncode


def selected_events(pool_dir: Path) -> list[dict]:
    events = build_events(load_pool_rows(pool_dir))
    keep = [event for event in events
            if event["scanCount"] >= MIN_SCAN_COUNT
            or (event["bestScan"].get("radarScore") or 0) >= MIN_RADAR_SCORE]
    keep.sort(key=lambda event: (-event["scanCount"],
                                 -(event["bestScan"].get("radarScore") or 0)))
    return keep[:MAX_EVENTS_PER_DAY]


def write_prediction_log(day_dir: Path, day: str, events: list[dict]) -> dict:
    """Immutable, hash-chained record of what the pipeline believed pre-review."""
    path = day_dir / "prediction-log.jsonl"
    if path.exists():
        return {"path": str(path), "reused": True}
    previous = "0" * 64
    lines = []
    for index, event in enumerate(events, 1):
        record = {
            "sequence": index,
            "harvestDay": day,
            "eventId": event["eventId"],
            "siteId": event["siteId"],
            "siteName": event.get("siteName"),
            "startAt": event["startAt"],
            "endAt": event["endAt"],
            "scanCount": event["scanCount"],
            "persistent": event["persistent"],
            "bestScan": event["bestScan"],
            "cameras": event["cameras"],
            "selectionPolicy": {"minScanCount": MIN_SCAN_COUNT,
                                "minRadarScore": MIN_RADAR_SCORE,
                                "maxEventsPerDay": MAX_EVENTS_PER_DAY},
            "note": "superset harvest; no ranking frozen; recorded before any human review",
        }
        body = json.dumps(record, sort_keys=True, separators=(",", ":"))
        record_hash = hashlib.sha256((previous + body).encode("utf-8")).hexdigest()
        lines.append(json.dumps({"prediction": record, "previousHash": previous,
                                 "recordHash": record_hash}) + "\n")
        previous = record_hash
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return {"path": str(path), "records": len(lines), "chainTerminus": previous}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="UTC date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--days-back", type=int, default=1,
                        help="harvest this many consecutive days ending at --date")
    parser.add_argument("--skip-frames", action="store_true",
                        help="pool and prediction log only; no FAA downloads")
    args = parser.parse_args()

    end = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
           if args.date else datetime.now(timezone.utc) - timedelta(days=1))
    summaries = []
    for offset in range(args.days_back - 1, -1, -1):
        day_start = (end - timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        day = day_start.strftime("%Y-%m-%d")
        day_dir = HARVEST_ROOT / day
        pool_dir = day_dir / "pool"
        queue_dir = day_dir / "queue"
        day_dir.mkdir(parents=True, exist_ok=True)
        print(f"== harvest {day}", flush=True)

        code = run([sys.executable, str(ROOT / "scripts/build_historical_candidate_pool.py"),
                    "--start", iso(day_start), "--end", iso(day_start + timedelta(days=1)),
                    "--output-dir", str(pool_dir)])
        if code != 0:
            summaries.append({"day": day, "status": f"pool_failed_{code}"})
            continue

        events = selected_events(pool_dir)
        log = write_prediction_log(day_dir, day, events)
        print(f"  selected {len(events)} events; prediction log {log.get('records', 'reused')}", flush=True)

        frames = None
        if not args.skip_frames and events:
            queue_dir.mkdir(parents=True, exist_ok=True)
            (queue_dir / "queue.json").write_text(
                json.dumps([{**event, "disposition": "harvest"} for event in events], indent=1) + "\n",
                encoding="utf-8")
            code = run([sys.executable, str(ROOT / "scripts/download_historical_review_frames.py"),
                        "--queue-dir", str(queue_dir), "--tiers", "harvest"])
            manifest = queue_dir / "frames-manifest.json"
            if manifest.exists():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                frames = sum(len(item.get("downloadedFrames") or []) for item in data["events"])
        summaries.append({"day": day, "status": "ok", "events": len(events), "frames": frames})
        print(f"  frames captured: {frames}", flush=True)

    state = {"schemaVersion": SCHEMA_VERSION, "completedAt": iso(datetime.now(timezone.utc)),
             "days": summaries}
    HARVEST_ROOT.mkdir(parents=True, exist_ok=True)
    (HARVEST_ROOT / f"harvest-run-{end.strftime('%Y-%m-%d')}.json").write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
