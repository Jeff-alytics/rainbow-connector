#!/usr/bin/env python
"""Evaluate hashed causal predictions against the pre-declared Gate 3 criteria."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from causal_replay import distance_km, verify_prediction_package

MODEL_ORDER = ("persistenceFirst", "boundedBlend", "radarFirst", "budgetGate")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prediction_records(package: Path):
    manifest = json.loads((package / "prediction-manifest.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = package / item["path"]
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)["prediction"]


def load_events(queue: Path) -> dict[str, dict]:
    events = {}
    for path in sorted(queue.glob("events*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                events[event["eventId"]] = event
    return events


def event_probe(event: dict) -> dict:
    scan = event["bestScan"]
    return {
        "eventId": event["eventId"],
        "bestScanAt": scan["observedAt"],
        "lat": scan["lat"], "lon": scan["lon"],
        "rainLat": scan["rainLat"], "rainLon": scan["rainLon"],
    }


def match_events_to_lineages(
    package: Path, probes: list[dict], observer_radius: float, rain_radius: float,
) -> tuple[dict[str, str], list[dict]]:
    by_time = defaultdict(list)
    for probe in probes:
        by_time[probe["bestScanAt"]].append(probe)
    matched, diagnostics = {}, []
    for record in prediction_records(package):
        wanted = by_time.get(record["scanTime"])
        if not wanted:
            continue
        for probe in wanted:
            choices = []
            for candidate in record["candidates"]:
                observer = distance_km(
                    probe["lat"], probe["lon"], candidate["lat"], candidate["lon"]
                )
                rain = distance_km(
                    probe["rainLat"], probe["rainLon"],
                    candidate["rainLat"], candidate["rainLon"],
                )
                if observer <= observer_radius and rain <= rain_radius:
                    choices.append((observer + rain, observer, rain, candidate))
            if choices:
                _, observer, rain, candidate = min(
                    choices, key=lambda item: (item[0], item[3]["lineageId"])
                )
                matched[probe["eventId"]] = candidate["lineageId"]
                diagnostics.append({
                    "eventId": probe["eventId"], "lineageId": candidate["lineageId"],
                    "observerDistanceKm": round(observer, 4),
                    "rainTargetDistanceKm": round(rain, 4),
                })
    return matched, diagnostics


def empty_point(model: str, threshold: float) -> dict:
    return {
        "model": model, "threshold": threshold, "totalAlerts": 0,
        "dailyAlerts": defaultdict(int), "cameraLaneAlerts": 0,
        "labelCrossings": {}, "frozenMissCrossings": {},
    }


def evaluate_thresholds(
    package: Path,
    thresholds: dict[str, list[float]],
    label_events_by_lineage: dict[str, list[str]],
    camera_lineages: set[str],
) -> tuple[dict[str, list[dict]], dict]:
    points = {
        model: [empty_point(model, threshold) for threshold in sorted(values)]
        for model, values in thresholds.items()
    }
    maximum_crossed: dict[str, dict[str, int]] = {
        model: {} for model in thresholds
    }
    support = {"candidates": 0, "supported": 0, "failed": 0, "unknown": 0}
    for record in prediction_records(package):
        day = record["scanTime"][:10]
        for candidate in record["candidates"]:
            support["candidates"] += 1
            state = candidate.get("spatialSupportState") or "unknown"
            support[state] = support.get(state, 0) + 1
            lineage = candidate["lineageId"]
            for model, model_points in points.items():
                outcome = candidate["models"][model]
                if not outcome["eligible"]:
                    continue
                values = thresholds[model]
                reached = bisect_right(values, float(outcome["score"])) - 1
                previous = maximum_crossed[model].get(lineage, -1)
                if reached <= previous:
                    continue
                for index in range(previous + 1, reached + 1):
                    point = model_points[index]
                    point["totalAlerts"] += 1
                    point["dailyAlerts"][day] += 1
                    if lineage in camera_lineages:
                        point["cameraLaneAlerts"] += 1
                    for event_id in label_events_by_lineage.get(lineage, ()):
                        point["labelCrossings"].setdefault(event_id, record["scanTime"])
                maximum_crossed[model][lineage] = reached
    return points, support


def evaluate_frozen_thresholds(
    package: Path, thresholds: dict[str, list[float]],
) -> dict[str, list[dict]]:
    points = {
        model: [empty_point(model, threshold) for threshold in sorted(values)]
        for model, values in thresholds.items()
    }
    maximum_crossed = {
        model: defaultdict(dict) for model in thresholds
    }
    for record in prediction_records(package):
        stream = record["streamId"]
        for candidate in record["candidates"]:
            lineage = candidate["lineageId"]
            for model, model_points in points.items():
                outcome = candidate["models"][model]
                if not outcome["eligible"]:
                    continue
                values = thresholds[model]
                reached = bisect_right(values, float(outcome["score"])) - 1
                previous = maximum_crossed[model][stream].get(lineage, -1)
                if reached <= previous:
                    continue
                for index in range(previous + 1, reached + 1):
                    point = model_points[index]
                    point["frozenMissCrossings"].setdefault(stream, record["scanTime"])
                maximum_crossed[model][stream][lineage] = reached
    return points


def point_summary(point: dict, targets: dict[str, dict], frozen_windows: dict[str, str]) -> dict:
    leads = {}
    for event_id, target in targets.items():
        crossing = point["labelCrossings"].get(event_id)
        leads[event_id] = None if not crossing else round(
            (parse_utc(target["targetAt"]) - parse_utc(crossing)).total_seconds() / 60, 2
        )
    frozen_leads = {}
    for stream, window_start in frozen_windows.items():
        crossing = point["frozenMissCrossings"].get(stream)
        frozen_leads[stream] = None if not crossing else round(
            (parse_utc(window_start) - parse_utc(crossing)).total_seconds() / 60, 2
        )
    daily = dict(sorted(point["dailyAlerts"].items()))
    return {
        "model": point["model"], "threshold": point["threshold"],
        "totalAlerts": point["totalAlerts"],
        "maximumDailyAlerts": max(daily.values(), default=0),
        "meanDailyAlerts": round(sum(daily.values()) / len(daily), 3) if daily else 0,
        "dailyAlerts": daily,
        "cameraLaneAlerts": point["cameraLaneAlerts"],
        "positiveLeadBowCount": sum(value is not None and value > 0 for value in leads.values()),
        "bowLeadsMinutes": leads,
        "frozenMissLeadsMinutes": frozen_leads,
    }


def baseline_summary(
    events: dict[str, dict], event_lineages: dict[str, str], targets: dict[str, dict],
) -> dict:
    alert_at = {}
    for event_id, event in events.items():
        if event.get("disposition") != "go" or event_id not in event_lineages:
            continue
        lineage = event_lineages[event_id]
        alert_at[lineage] = min(alert_at.get(lineage, event["startAt"]), event["startAt"])
    leads = {}
    for event_id, target in targets.items():
        lineage = event_lineages.get(event_id)
        crossing = alert_at.get(lineage)
        leads[event_id] = None if not crossing else round(
            (parse_utc(target["targetAt"]) - parse_utc(crossing)).total_seconds() / 60, 2
        )
    return {
        "uniqueLineageAlerts": len(alert_at),
        "positiveLeadBowCount": sum(value is not None and value > 0 for value in leads.values()),
        "bowLeadsMinutes": leads,
    }


def location_delivery_metrics(alerts: list[dict], radius_km: float, cooldown_hours: float) -> dict:
    counts = []
    for anchor in alerts:
        start = parse_utc(anchor["scanTime"])
        nearby = 0
        for alert in alerts:
            delta = (parse_utc(alert["scanTime"]) - start).total_seconds() / 3600
            if 0 <= delta <= cooldown_hours and distance_km(
                anchor["lat"], anchor["lon"], alert["lat"], alert["lon"]
            ) <= radius_km:
                nearby += 1
        counts.append(nearby)
    counts.sort()
    def percentile(fraction):
        return counts[min(len(counts) - 1, math.floor(fraction * len(counts)))] if counts else 0
    return {
        "affectedAlertCenters": len(counts),
        "medianAlertsWithinCooldown": percentile(0.5),
        "p95AlertsWithinCooldown": percentile(0.95),
        "maximumAlertsWithinCooldown": max(counts, default=0),
    }


def collect_alerts(package: Path, model: str, threshold: float) -> list[dict]:
    seen, alerts = set(), []
    for record in prediction_records(package):
        for candidate in record["candidates"]:
            outcome = candidate["models"][model]
            lineage = candidate["lineageId"]
            if lineage in seen or not outcome["eligible"] or float(outcome["score"]) < threshold:
                continue
            seen.add(lineage)
            alerts.append({
                "lineageId": lineage, "scanTime": record["scanTime"],
                "lat": candidate["lat"], "lon": candidate["lon"],
            })
    return alerts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--frozen-predictions", type=Path, required=True)
    parser.add_argument("--gate-config", type=Path, default=ROOT / "validation/causal-replay-gate/gate-config.json")
    parser.add_argument("--targets", type=Path, default=ROOT / "validation/causal-replay-gate/bow-evaluation-targets.json")
    parser.add_argument("--queue", type=Path, default=ROOT / "validation/historical-review-queue")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    integrity = {
        "nationwide": verify_prediction_package(args.predictions),
        "frozenMisses": verify_prediction_package(args.frozen_predictions),
    }
    if not all(item["ok"] for item in integrity.values()):
        raise RuntimeError(f"Prediction package integrity failed: {integrity}")
    config = json.loads(args.gate_config.read_text(encoding="utf-8"))
    target_payload = json.loads(args.targets.read_text(encoding="utf-8"))
    targets = {item["eventId"]: item for item in target_payload["targets"]}
    events = load_events(args.queue)
    probes = [event_probe(event) for event in events.values()]
    match_config = config["labelLineageMatch"]
    event_lineages, match_diagnostics = match_events_to_lineages(
        args.predictions, probes,
        match_config["maximumObserverDistanceKm"],
        match_config["maximumRainTargetDistanceKm"],
    )
    label_events_by_lineage = defaultdict(list)
    for event_id in targets:
        if event_id in event_lineages:
            label_events_by_lineage[event_lineages[event_id]].append(event_id)
    camera_lineages = set(event_lineages.values())
    thresholds = {
        model: sorted(float(value) for value in config["thresholds"][model])
        for model in MODEL_ORDER
    }
    points, support = evaluate_thresholds(
        args.predictions, thresholds, label_events_by_lineage, camera_lineages,
    )
    frozen_points = evaluate_frozen_thresholds(args.frozen_predictions, thresholds)
    frozen_manifest = json.loads(
        (args.frozen_predictions / "prediction-manifest.json").read_text(encoding="utf-8")
    )
    frozen_windows = {
        item["streamId"]: item["window"][0]
        for item in frozen_manifest["streamSummaries"] if item.get("window")
    }
    summaries = []
    point_lookup = {}
    for model in MODEL_ORDER:
        for point, frozen_point in zip(points[model], frozen_points[model]):
            point["frozenMissCrossings"] = frozen_point["frozenMissCrossings"]
            summary = point_summary(point, targets, frozen_windows)
            summaries.append(summary)
            point_lookup[(model, float(point["threshold"]))] = summary

    baseline = baseline_summary(events, event_lineages, targets)
    tolerance = config["comparativeBaseline"]["maximumVolumeDifference"]
    comparisons = {}
    for model in MODEL_ORDER:
        choices = [item for item in summaries if item["model"] == model]
        matched = min(
            choices,
            key=lambda item: (
                abs(item["cameraLaneAlerts"] - baseline["uniqueLineageAlerts"]),
                -item["positiveLeadBowCount"], -item["threshold"],
            ),
        )
        volume_difference = abs(
            matched["cameraLaneAlerts"] - baseline["uniqueLineageAlerts"]
        )
        comparisons[model] = {
            "threshold": matched["threshold"],
            "newCameraLaneAlerts": matched["cameraLaneAlerts"],
            "baselineAlerts": baseline["uniqueLineageAlerts"],
            "volumeDifference": volume_difference,
            "withinTolerance": volume_difference <= tolerance,
            "newPositiveLeadBowCount": matched["positiveLeadBowCount"],
            "baselinePositiveLeadBowCount": baseline["positiveLeadBowCount"],
            "strictlyDominates": (
                volume_difference <= tolerance
                and matched["positiveLeadBowCount"] > baseline["positiveLeadBowCount"]
            ),
        }

    operating = config["operatingPoint"]
    recall_gate = config["recallGate"]
    required_misses = recall_gate["requiredFrozenMissIds"]
    qualifying = []
    for item in summaries:
        item["passesAlertBudget"] = (
            item["maximumDailyAlerts"] <= operating["maximumNationwideAlertsPerUtcDay"]
        )
        item["passesBowRecall"] = (
            item["positiveLeadBowCount"] >= recall_gate["minimumLabeledBowsWithPositiveLead"]
        )
        item["passesFrozenMisses"] = all(
            item["frozenMissLeadsMinutes"].get(case) is not None
            and item["frozenMissLeadsMinutes"][case] >= recall_gate["frozenMissMinimumLeadMinutes"]
            for case in required_misses
        )
        item["passesComparative"] = comparisons[item["model"]]["strictlyDominates"]
        if all(item[key] for key in (
            "passesAlertBudget", "passesBowRecall",
            "passesFrozenMisses", "passesComparative",
        )):
            qualifying.append(item)
    selected = None
    if qualifying:
        selected = min(
            qualifying,
            key=lambda item: (
                -item["positiveLeadBowCount"], item["maximumDailyAlerts"],
                item["totalAlerts"], MODEL_ORDER.index(item["model"]), -item["threshold"],
            ),
        )
        alerts = collect_alerts(args.predictions, selected["model"], selected["threshold"])
        selected["locationDeliveryMetrics"] = location_delivery_metrics(
            alerts, operating["locationAlertRadiusKm"], operating["locationCooldownHours"]
        )
        selected["passesTypicalLocationFrequency"] = (
            selected["locationDeliveryMetrics"]["medianAlertsWithinCooldown"] <= 1
        )

    support_gate = (
        support["candidates"] > 0 and support["unknown"] == 0 and support["failed"] > 0
    )
    gate_passed = (
        bool(selected) and support_gate
        and selected["passesTypicalLocationFrequency"]
    )
    report = {
        "schemaVersion": "causal-replay-gate-report.v1",
        "gatePassed": gate_passed,
        "selectedOperatingPoint": selected,
        "supportGate": {**support, "active": support_gate},
        "labelMatching": {
            "events": len(events), "matchedEvents": len(event_lineages),
            "bowTargets": len(targets),
            "matchedBowTargets": sum(event_id in event_lineages for event_id in targets),
            "diagnostics": match_diagnostics,
        },
        "v1Baseline": baseline,
        "matchedVolumeComparisons": comparisons,
        "volumeCurves": summaries,
        "integrity": integrity,
        "inputs": {
            "gateConfig": {"path": str(args.gate_config), "sha256": sha256(args.gate_config)},
            "targets": {"path": str(args.targets), "sha256": sha256(args.targets)},
            "nationwideManifest": sha256(args.predictions / "prediction-manifest.json"),
            "frozenManifest": sha256(args.frozen_predictions / "prediction-manifest.json"),
        },
        "caveats": [
            "July is a development/model-selection set, not prospective validation.",
            "First marked frames upper-bound onset; six correction targets are machine-ranked and human-confirmed.",
            "The V1 alert time uses event startAt and therefore favors V1 where exact per-scan GO onset is unavailable.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "gatePassed": gate_passed,
        "selectedOperatingPoint": selected,
        "matchedBowTargets": report["labelMatching"]["matchedBowTargets"],
        "supportGate": report["supportGate"],
        "output": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
