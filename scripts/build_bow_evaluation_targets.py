#!/usr/bin/env python
"""Freeze the 35 July bow lead-time targets before replay results are read."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "validation" / "historical-review-queue"
OUTPUT = ROOT / "validation" / "causal-replay-gate" / "bow-evaluation-targets.json"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_events() -> dict[str, dict]:
    events = {}
    for path in sorted(QUEUE.glob("events*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                events[event["eventId"]] = event
    return events


def build_payload() -> dict:
    round1_path = QUEUE / "human-labels-round1-2026-08-02.json"
    round3_path = QUEUE / "human-labels-round3-2026-08-02.json"
    corrections_path = QUEUE / "human-labels-corrections-2026-08-02.json"
    machine_path = QUEUE / "machine-reviewer-clip.jsonl"
    round1, round3, corrections = map(
        load_json, (round1_path, round3_path, corrections_path)
    )
    events = load_events()
    machine = {}
    filename_times = {}
    for line in machine_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        machine[row["eventId"]] = row
        for frame in row.get("frames") or []:
            filename_times[(row["eventId"], frame["file"])] = frame["observedAt"]

    marked_files = [
        label
        for payload in (round1, round3)
        for label in payload.get("labels") or []
        if label.get("label") == "rainbow"
    ]
    marked_times: dict[str, list[str]] = {}
    for label in marked_files:
        parts = label["file"].replace(chr(92), "/").split("/")
        event_id, filename = parts[-2], parts[-1]
        observed_at = filename_times.get((event_id, filename))
        if observed_at:
            marked_times.setdefault(event_id, []).append(observed_at)

    bow_ids = set(marked_times)
    bow_ids.update(
        item["eventId"]
        for item in round3.get("eventLabels") or []
        if item.get("label") == "rainbow"
    )
    correction_ids = {
        item["eventId"] for item in corrections.get("events") or []
        if item.get("label") == "rainbow"
    }
    bow_ids.update(correction_ids)

    targets = []
    for event_id in sorted(bow_ids):
        event = events[event_id]
        if marked_times.get(event_id):
            target_at = min(marked_times[event_id])
            proxy = "first_human_marked_rainbow_frame"
        elif event_id in correction_ids:
            frames = machine[event_id].get("frames") or []
            frame = max(
                frames,
                key=lambda item: (
                    float(item.get("contrastiveRainbowProb") or 0),
                    float(item.get("rainbowSimilarity") or 0),
                    item["observedAt"],
                ),
            )
            target_at = frame["observedAt"]
            proxy = "machine_ranked_frame_confirmed_by_human_correction"
        else:
            target_at = event["bestScan"]["observedAt"]
            proxy = "event_best_scan_upper_bound"
        scan = event["bestScan"]
        targets.append({
            "eventId": event_id,
            "targetAt": target_at,
            "leadProxy": proxy,
            "eventStartAt": event["startAt"],
            "eventEndAt": event["endAt"],
            "bestScanAt": scan["observedAt"],
            "lat": scan["lat"],
            "lon": scan["lon"],
            "rainLat": scan["rainLat"],
            "rainLon": scan["rainLon"],
            "v1Disposition": event.get("disposition"),
            "v1OptimisticAlertAt": event["startAt"] if event.get("disposition") == "go" else None,
        })

    if len(targets) != 35:
        raise RuntimeError(f"Expected 35 bow targets, found {len(targets)}")
    proxy_counts = {}
    for target in targets:
        proxy_counts[target["leadProxy"]] = proxy_counts.get(target["leadProxy"], 0) + 1
    payload = {
        "schemaVersion": "bow-evaluation-targets.v1",
        "createdBeforeReplayEvaluation": True,
        "targetCount": len(targets),
        "leadProxyCounts": proxy_counts,
        "sources": [
            {"path": path.relative_to(ROOT).as_posix(), "sha256": file_hash(path)}
            for path in (round1_path, round3_path, corrections_path, machine_path)
        ],
        "targets": targets,
    }
    return payload


def write_frozen_payload(payload: dict, output: Path, overwrite: bool = False) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with output.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Explicitly replace an existing output (never the default).",
    )
    args = parser.parse_args()
    payload = build_payload()
    write_frozen_payload(payload, args.output, overwrite=args.force)
    print(json.dumps({
        "output": str(args.output),
        "proxyCounts": payload["leadProxyCounts"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
