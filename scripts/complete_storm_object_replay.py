#!/usr/bin/env python
"""Wait for resumable decode shards, then package and evaluate exactly once."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = Path("C:/tmp/storm-object-scan-artifacts")
GATE_ROOT = ROOT / "validation" / "storm-object-replay-gate"
ARTIFACT_MANIFEST = GATE_ROOT / "scan-artifact-manifest.json"
PREDICTIONS = GATE_ROOT / "july-predictions"
REPORT = GATE_ROOT / "gate-report.json"


def expected_scans() -> int:
    count = 0
    for path in (ROOT / "validation" / "historical-camera-free-pool").glob("shard-*/pool-*.jsonl"):
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    row = json.loads(line)
                    count += row.get("status") == "ok" and "allSeeds" in row
    return count


def run(arguments: list[str]) -> None:
    print(json.dumps({"running": arguments}), flush=True)
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def main() -> int:
    expected = expected_scans()
    print(json.dumps({"expectedArtifacts": expected}), flush=True)
    while True:
        complete = sum(1 for _ in ARTIFACT_ROOT.glob("shard-*/*/scan-*.json.gz"))
        temporary = sum(1 for _ in ARTIFACT_ROOT.rglob("*.tmp"))
        print(json.dumps({"completeArtifacts": complete, "temporaryArtifacts": temporary}), flush=True)
        if complete == expected and temporary == 0:
            break
        if complete > expected:
            raise RuntimeError(f"Artifact count exceeds frozen input count: {complete} > {expected}")
        time.sleep(30)
    run([
        "scripts/run_storm_object_replay.py",
        "--artifact-root", str(ARTIFACT_ROOT),
        "--artifact-manifest", str(ARTIFACT_MANIFEST),
        "--build-artifact-manifest",
    ])
    run([
        "scripts/run_storm_object_replay.py",
        "--artifact-manifest", str(ARTIFACT_MANIFEST),
        "--output-dir", str(PREDICTIONS),
    ])
    run([
        "scripts/evaluate_storm_object_replay.py",
        "--artifact-manifest", str(ARTIFACT_MANIFEST),
        "--predictions", str(PREDICTIONS),
        "--output", str(REPORT),
    ])
    print(json.dumps({"done": True, "report": str(REPORT)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
