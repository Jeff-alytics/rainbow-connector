#!/usr/bin/env python
"""Evaluate token-granted storm-object alerts against the frozen July targets."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "worker"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from opportunity_ledger import observer_swath, swath_contains
from scripts.run_storm_object_replay import file_sha256, verify_artifact_manifest, verify_prediction_package
from scripts.storm_object_replay import content_sha256, load_artifact, parse_utc

SCHEMA_VERSION = "storm-object-gate-report.v1"


def distance_km(lat1, lon1, lat2, lon2) -> float:
    north = (float(lat1) - float(lat2)) * 111
    east = (float(lon1) - float(lon2)) * 111 * math.cos(math.radians((float(lat1) + float(lat2)) / 2))
    return math.hypot(north, east)


def nearest_scan(target_at: str, sources: list[dict], maximum_minutes: float = 5) -> dict | None:
    target = parse_utc(target_at)
    best = min(sources, key=lambda item: abs((parse_utc(item["observedAt"]) - target).total_seconds()))
    gap = abs((parse_utc(best["observedAt"]) - target).total_seconds()) / 60
    return best if gap <= maximum_minutes else None


def distance_to_object_km(target_lat: float, target_lon: float, item: dict, grid: dict) -> float:
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    target_row = (float(target_lat) - lat0) / dlat
    target_column = (float(target_lon) - lon0) / dlon
    best = math.inf
    for row, first, last, *_ in item.get("runs") or []:
        row = int(row)
        column = min(max(target_column, int(first)), int(last))
        lat = lat0 + row * dlat
        lon = lon0 + column * dlon
        best = min(best, distance_km(target_lat, target_lon, lat, lon))
    return best


def assign_bow_target(target: dict, source: dict) -> dict:
    artifact = load_artifact(Path(source["path"]))
    near = []
    for item in artifact["stormObjects"]:
        rain_distance = distance_to_object_km(target["rainLat"], target["rainLon"], item, artifact["grid"])
        if rain_distance <= 3:
            near.append((rain_distance, item))
    matches = []
    sidecar = {"grid": artifact["grid"]}
    observed_at = parse_utc(artifact["observedAt"])
    for rain_distance, item in sorted(near, key=lambda pair: (pair[0], pair[1]["componentId"])):
        swath = observer_swath(item, sidecar, observed_at)
        inside, observer_distance = swath_contains(swath, target["lat"], target["lon"])
        if inside:
            matches.append((rain_distance, observer_distance or 0.0, item))
    if not matches:
        return {
            "id": target["eventId"],
            "kind": "labeled_bow",
            "targetAt": target["targetAt"],
            "scanTime": artifact["observedAt"],
            "componentId": None,
            "assignment": "unmatched",
            "nearRainObjects": len(near),
        }
    rain_distance, observer_distance, item = min(matches, key=lambda value: (
        value[0], value[1], value[2]["componentId"]
    ))
    exact_seed = False
    for attachment in artifact.get("attachedSeeds") or []:
        if attachment["stormComponentId"] != item["componentId"]:
            continue
        seed = attachment["seed"]
        if (
            distance_km(target["lat"], target["lon"], seed["lat"], seed["lon"]) <= 2
            and distance_km(target["rainLat"], target["rainLon"], seed["rainLat"], seed["rainLon"]) <= 2
        ):
            exact_seed = True
            break
    return {
        "id": target["eventId"],
        "kind": "labeled_bow",
        "targetAt": target["targetAt"],
        "scanTime": artifact["observedAt"],
        "componentId": item["componentId"],
        "assignment": "production_swath_and_envelope",
        "rainEnvelopeDistanceKm": round(rain_distance, 3),
        "observerSwathDistanceKm": round(observer_distance, 3),
        "exactSeedDiagnostic": exact_seed,
    }


def assign_frozen_case(case: dict, source: dict) -> dict:
    artifact = load_artifact(Path(source["path"]))
    matches = []
    for attachment in artifact.get("attachedSeeds") or []:
        seed = attachment["seed"]
        observer_distance = min(
            distance_km(seed["lat"], seed["lon"], observation["lat"], observation["lon"])
            for observation in case["observations"]
        )
        if observer_distance <= 15:
            matches.append((
                -float(seed.get("radarScore") or 0),
                observer_distance,
                attachment["stormComponentId"],
            ))
    if not matches:
        component_id = None
        assignment = "unmatched"
        observer_distance = None
    else:
        _, observer_distance, component_id = min(matches)
        assignment = "case_seed_within_15km_attached_to_family"
    return {
        "id": case["id"],
        "kind": "frozen_miss",
        "targetAt": case["window"][0],
        "scanTime": artifact["observedAt"],
        "componentId": component_id,
        "assignment": assignment,
        "observerDistanceKm": round(observer_distance, 3) if observer_distance is not None else None,
    }


def load_prediction_records(output_dir: Path):
    manifest = json.loads((output_dir / "prediction-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        with (output_dir / item["path"]).open("rb") as stream:
            for raw in stream:
                yield json.loads(raw)["prediction"]


def rolling_24h_max(alerts: list[dict]) -> int:
    window = deque()
    best = 0
    for alert in sorted(alerts, key=lambda item: item["alertAt"]):
        at = parse_utc(alert["alertAt"])
        while window and at - window[0] > timedelta(hours=24):
            window.popleft()
        window.append(at)
        best = max(best, len(window))
    return best


def evaluate_arm(assignments: list[dict], alerts: list[dict], eligible: list[dict], capacity: int, threshold: float) -> dict:
    target_results = {}
    for target in assignments:
        family = set(target.get("familyIds") or [])
        target_at = parse_utc(target["targetAt"])
        matching_alerts = [
            item for item in alerts
            if family & set(item["familyIds"])
        ]
        matching_eligible = [
            item for item in eligible
            if item["score"] >= threshold and family & set(item["familyIds"])
        ]
        first_alert = min(matching_alerts, key=lambda item: item["alertAt"]) if matching_alerts else None
        first_eligible = min(matching_eligible, key=lambda item: item["scanTime"]) if matching_eligible else None
        lead = (
            (target_at - parse_utc(first_alert["alertAt"])).total_seconds() / 60
            if first_alert else None
        )
        eligible_lead = (
            (target_at - parse_utc(first_eligible["scanTime"])).total_seconds() / 60
            if first_eligible else None
        )
        target_results[target["id"]] = {
            "scheduledAlertAt": first_alert["alertAt"] if first_alert else None,
            "scheduledLeadMinutes": round(lead, 2) if lead is not None else None,
            "firstEligibleAt": first_eligible["scanTime"] if first_eligible else None,
            "eligibilityLeadMinutes": round(eligible_lead, 2) if eligible_lead is not None else None,
            "budgetLost": bool(eligible_lead is not None and eligible_lead > 0 and not (lead is not None and lead > 0)),
        }
    labeled = [item for item in assignments if item["kind"] == "labeled_bow"]
    frozen = [item for item in assignments if item["kind"] == "frozen_miss"]
    positive = sum(
        (target_results[item["id"]]["scheduledLeadMinutes"] or -math.inf) > 0
        for item in labeled
    )
    budget_lost = sum(target_results[item["id"]]["budgetLost"] for item in labeled)
    frozen_pass = all(
        (target_results[item["id"]]["scheduledLeadMinutes"] or -math.inf) >= 10
        for item in frozen
    )
    return {
        "capacity": capacity,
        "threshold": threshold,
        "totalScheduledAlerts": len(alerts),
        "maximumAlertsAnyRolling24Hours": rolling_24h_max(alerts),
        "scheduledPositiveLeadBowCount": positive,
        "budgetLostBowCount": budget_lost,
        "frozenMissLeadGatePassed": frozen_pass,
        "targetResults": target_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "validation/storm-object-replay-gate/gate-config-v2.json")
    parser.add_argument("--targets", type=Path, default=ROOT / "validation/causal-replay-gate/bow-evaluation-targets.json")
    parser.add_argument("--frozen-cases", type=Path, default=ROOT / "validation/frozen-case-pool-check/camera-free-seeds.json")
    parser.add_argument("--prior-gate-report", type=Path, default=ROOT / "validation/causal-replay-gate/gate-report.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact_verification = verify_artifact_manifest(args.artifact_manifest)
    prediction_verification = verify_prediction_package(args.predictions)
    if not artifact_verification["ok"] or not prediction_verification["ok"]:
        raise ValueError("Input package verification failed")
    artifact_manifest = json.loads(args.artifact_manifest.read_text(encoding="utf-8"))
    sources = artifact_manifest["files"]
    targets = json.loads(args.targets.read_text(encoding="utf-8"))["targets"]
    cases = json.loads(args.frozen_cases.read_text(encoding="utf-8-sig"))["cases"]
    assignments = []
    for target in targets:
        source = nearest_scan(target["targetAt"], sources)
        assignments.append(
            assign_bow_target(target, source)
            if source else {
                "id": target["eventId"], "kind": "labeled_bow",
                "targetAt": target["targetAt"], "assignment": "no_scan_within_5min",
                "componentId": None, "scanTime": None,
            }
        )
    for case in cases[:3]:
        source = nearest_scan(case["window"][0], sources)
        assignments.append(
            assign_frozen_case(case, source)
            if source else {
                "id": case["id"], "kind": "frozen_miss",
                "targetAt": case["window"][0], "assignment": "no_scan_within_5min",
                "componentId": None, "scanTime": None,
            }
        )
    assignment_by_scan = defaultdict(list)
    for item in assignments:
        if item.get("scanTime"):
            assignment_by_scan[item["scanTime"]].append(item)
    alerts_by_arm = defaultdict(list)
    eligible = []
    diagnostics = defaultdict(int)
    for record in load_prediction_records(args.predictions):
        for target in assignment_by_scan.get(record["scanTime"], []):
            lineage = next((
                item for item in record["objectLineage"]
                if item["componentId"] == target["componentId"]
            ), None)
            target["familyIds"] = sorted(set(
                ([lineage["eventId"], *lineage["ancestorEventIds"]]) if lineage else []
            ))
        for family in record["stormFamilies"]:
            if family["eligible"]:
                eligible.append({
                    "scanTime": record["scanTime"],
                    "score": family["score"],
                    "familyIds": sorted(set([family["eventId"], *family["ancestorEventIds"]])),
                })
        for alert in record["schedulerAlerts"]:
            key = int(alert["capacity"]), float(alert["threshold"])
            alerts_by_arm[key].append({
                **alert,
                "familyIds": sorted(set([alert["eventId"], *alert["ancestorEventIds"]])),
            })
        diagnostics["scans"] += 1
        diagnostics["stormObjects"] += record["diagnostics"]["stormObjects"]
        diagnostics["unattachedSeeds"] += record["diagnostics"]["unattachedSeeds"]
        for band, count in record["diagnostics"]["seedAttachmentCoveragePerObject"].items():
            diagnostics[f"objectsWith{band}AttachedSeeds"] += count
    config = json.loads(args.config.read_text(encoding="utf-8"))
    arms = [
        evaluate_arm(assignments, alerts_by_arm[(capacity, float(threshold))], eligible, capacity, float(threshold))
        for capacity in sorted({
            config["scheduler"]["primaryBucketCapacity"],
            *config["scheduler"]["sensitivityBucketCapacities"],
        })
        for threshold in config["eligibility"]["scoreThresholds"]
    ]
    best_by_capacity = {}
    for capacity in sorted({item["capacity"] for item in arms}):
        best_by_capacity[capacity] = max(
            (item for item in arms if item["capacity"] == capacity),
            key=lambda item: (item["scheduledPositiveLeadBowCount"], item["threshold"]),
        )
    selected = best_by_capacity[3]
    alternatives = [
        item for capacity, item in best_by_capacity.items()
        if capacity != 3
        and item["scheduledPositiveLeadBowCount"] > selected["scheduledPositiveLeadBowCount"]
        and item["budgetLostBowCount"] <= selected["budgetLostBowCount"]
    ]
    if alternatives:
        selected = max(alternatives, key=lambda item: (
            item["scheduledPositiveLeadBowCount"],
            -item["budgetLostBowCount"],
            -abs(item["capacity"] - 3),
        ))
    baseline = json.loads(args.prior_gate_report.read_text(encoding="utf-8"))["v1Baseline"]
    matched = min(arms, key=lambda item: (
        abs(item["totalScheduledAlerts"] - baseline["uniqueLineageAlerts"]),
        -item["scheduledPositiveLeadBowCount"],
    ))
    matched_difference = abs(matched["totalScheduledAlerts"] - baseline["uniqueLineageAlerts"])
    comparative_pass = (
        matched_difference <= 1
        and matched["scheduledPositiveLeadBowCount"] > baseline["positiveLeadBowCount"]
    )
    rolling_bound = selected["capacity"] + config["scheduler"]["tokensPer24Hours"]
    scheduler_pass = selected["maximumAlertsAnyRolling24Hours"] <= rolling_bound
    gate_passed = (
        selected["scheduledPositiveLeadBowCount"] >= config["bindingEvidence"]["minimumLabeledBowsWithPositiveScheduledLead"]
        and selected["frozenMissLeadGatePassed"]
        and comparative_pass
        and scheduler_pass
    )
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "gatePassed": gate_passed,
        "selectedOperatingPoint": selected,
        "bestByCapacity": best_by_capacity,
        "capacitySelectionRule": config["scheduler"]["capacitySelectionRule"],
        "thresholdSelectionRule": config["scheduler"]["thresholdSelectionRule"],
        "schedulerCorrectness": {
            "passed": scheduler_pass,
            "rolling24HourBound": rolling_bound,
            "observedMaximum": selected["maximumAlertsAnyRolling24Hours"],
            "perUtcDayCountsAreReportingOnly": True,
        },
        "comparative": {
            "passed": comparative_pass,
            "v1Baseline": baseline,
            "nearestMatchedArm": matched,
            "volumeDifference": matched_difference,
        },
        "targetAssignments": assignments,
        "allArms": arms,
        "diagnostics": dict(sorted(diagnostics.items())),
        "integrity": {
            "artifactPackageVerified": True,
            "predictionPackageVerified": True,
            "configSha256": file_sha256(args.config),
            "targetsSha256": file_sha256(args.targets),
        },
        "caveats": [
            "July remains development/model-selection data.",
            "Marked rainbow frames upper-bound unknown physical onset.",
            "Unmatched primary swath/envelope assignments count as misses.",
        ],
    }
    report["reportContentSha256"] = content_sha256(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline=chr(10)) as stream:
        stream.write(json.dumps(report, indent=2) + chr(10))
    print(json.dumps({
        "output": str(args.output),
        "gatePassed": gate_passed,
        "selected": {
            key: selected[key]
            for key in (
                "capacity", "threshold", "totalScheduledAlerts",
                "scheduledPositiveLeadBowCount", "budgetLostBowCount",
                "frozenMissLeadGatePassed",
            )
        },
        "reportContentSha256": report["reportContentSha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
