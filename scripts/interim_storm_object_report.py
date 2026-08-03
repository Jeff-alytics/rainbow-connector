#!/usr/bin/env python
"""Read only closed prediction days and print a non-gating interim diagnostic."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "worker"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts.evaluate_storm_object_replay import (
    assign_bow_target,
    evaluate_arm,
    nearest_scan,
)


def main() -> int:
    prediction_dir = ROOT / "validation" / "storm-object-replay-gate" / "july-predictions"
    files = sorted(path for path in prediction_dir.glob("predictions-*.jsonl") if path.stat().st_size > 0)
    # The newest nonempty file may still be open. A later day's existence proves
    # every earlier file was closed by the replay writer.
    all_days = sorted(prediction_dir.glob("predictions-*.jsonl"))
    if all_days:
        current_day = all_days[-1].stem.removeprefix("predictions-")
        files = [path for path in files if path.stem.removeprefix("predictions-") < current_day]
    if not files:
        raise RuntimeError("No closed prediction day is available")
    last_day = files[-1].stem.removeprefix("predictions-")
    artifact_manifest = json.loads(
        (ROOT / "validation" / "storm-object-replay-gate" / "scan-artifact-manifest.json").read_text(encoding="utf-8")
    )
    sources = [item for item in artifact_manifest["files"] if item["observedAt"][:10] <= last_day]
    targets = [
        item for item in json.loads(
            (ROOT / "validation" / "causal-replay-gate" / "bow-evaluation-targets.json").read_text(encoding="utf-8")
        )["targets"]
        if item["targetAt"][:10] <= last_day
    ]
    assignments = []
    for target in targets:
        source = nearest_scan(target["targetAt"], sources)
        assignments.append(assign_bow_target(target, source) if source else {
            "id": target["eventId"], "kind": "labeled_bow",
            "targetAt": target["targetAt"], "scanTime": None,
            "componentId": None, "assignment": "no_scan_within_5min",
        })
    assignment_by_scan = defaultdict(list)
    for item in assignments:
        if item.get("scanTime"):
            assignment_by_scan[item["scanTime"]].append(item)
    alerts_by_arm = defaultdict(list)
    eligible = []
    diagnostics = defaultdict(int)
    for path in files:
        with path.open("rb") as stream:
            for raw in stream:
                record = json.loads(raw)["prediction"]
                for target in assignment_by_scan.get(record["scanTime"], []):
                    lineage = next((
                        item for item in record["objectLineage"]
                        if item["componentId"] == target["componentId"]
                    ), None)
                    target["familyIds"] = sorted(set(
                        ([lineage["eventId"], *lineage["ancestorEventIds"]]) if lineage else []
                    ))
                for family in record["stormFamilies"]:
                    eligible.append({
                        "scanTime": record["scanTime"],
                        "score": family["score"],
                        "familyIds": sorted(set([family["eventId"], *family["ancestorEventIds"]])),
                    })
                for alert in record["schedulerAlerts"]:
                    alerts_by_arm[(int(alert["capacity"]), float(alert["threshold"]))].append({
                        **alert,
                        "familyIds": sorted(set([alert["eventId"], *alert["ancestorEventIds"]])),
                    })
                diagnostics["scans"] += 1
                diagnostics["stormObjects"] += record["diagnostics"]["stormObjects"]
                diagnostics["unattachedSeeds"] += record["diagnostics"]["unattachedSeeds"]
                for band, count in record["diagnostics"]["seedAttachmentCoveragePerObject"].items():
                    diagnostics[f"objectsWith{band}AttachedSeeds"] += count
    config = json.loads(
        (ROOT / "validation" / "storm-object-replay-gate" / "gate-config-v2.json").read_text(encoding="utf-8")
    )
    arms = [
        evaluate_arm(assignments, alerts_by_arm[(capacity, float(threshold))], eligible, capacity, float(threshold))
        for capacity in (2, 3, 4)
        for threshold in config["eligibility"]["scoreThresholds"]
    ]
    best = {
        capacity: max(
            (item for item in arms if item["capacity"] == capacity),
            key=lambda item: (item["scheduledPositiveLeadBowCount"], item["threshold"]),
        )
        for capacity in (2, 3, 4)
    }
    primary = best[3]
    print(json.dumps({
        "status": "INTERIM_NON_GATING",
        "closedWindow": {
            "start": sources[0]["observedAt"],
            "end": sources[-1]["observedAt"],
            "days": len(files),
            "scans": diagnostics["scans"],
        },
        "labeledBowsAvailable": len(assignments),
        "matchedTargetFamilies": sum(bool(item.get("familyIds")) for item in assignments),
        "targetAssignmentDiagnostics": {
            "assignmentStatuses": {
                status: sum(item.get("assignment") == status for item in assignments)
                for status in sorted({item.get("assignment") for item in assignments})
            },
            "componentAssignedButNoAttachedLineage": sum(
                bool(item.get("componentId")) and not item.get("familyIds")
                for item in assignments
            ),
            "noComponentAssigned": sum(not item.get("componentId") for item in assignments),
            "exactSeedDiagnosticMatches": sum(
                item.get("exactSeedDiagnostic") is True for item in assignments
            ),
        },
        "primaryCapacityBestThresholdSoFar": {
            key: primary[key] for key in (
                "capacity", "threshold", "totalScheduledAlerts",
                "maximumAlertsAnyRolling24Hours",
                "scheduledPositiveLeadBowCount", "budgetLostBowCount",
            )
        },
        "bestByCapacity": {
            str(capacity): {
                key: item[key] for key in (
                    "threshold", "totalScheduledAlerts",
                    "scheduledPositiveLeadBowCount", "budgetLostBowCount",
                )
            }
            for capacity, item in best.items()
        },
        "diagnostics": dict(sorted(diagnostics.items())),
        "notEvaluated": [
            "July 17-30 bows",
            "all three frozen misses",
            "matched-volume V1 comparison",
            "final capacity selection",
            "Gate verdict"
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
