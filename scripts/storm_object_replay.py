"""Causal scoring and token scheduling for ledger storm-object artifacts."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from opportunity_ledger import link_lineage

SCHEMA_VERSION = "storm-object-causal-replay.v1"
MODEL_VERSION = "causal-bounded-persistence-radar-v2"
SCHEDULER_VERSION = "nationwide-causal-token-bucket-2026-08-v2"


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def sunlight_modifier(seed: dict) -> tuple[float, str]:
    sunlight = seed.get("sunlightV2") or {}
    state = sunlight.get("sunlightState") or seed.get("sunlightState") or "unavailable"
    if state == "sunlit_supported":
        return 3.0, state
    if state == "overcast_supported" and sunlight.get("evidencePresent") is True:
        return -10.0, state
    return 0.0, state


def valid_seed(seed: dict) -> tuple[bool, list[str]]:
    reasons = []
    elevation = finite(seed.get("sunElevationDeg"))
    required = {
        "observer_location": finite(seed.get("lat")) is not None and finite(seed.get("lon")) is not None,
        "rain_location": finite(seed.get("rainLat")) is not None and finite(seed.get("rainLon")) is not None,
        "radar_score": finite(seed.get("radarScore")) is not None,
        "sun_geometry": elevation is not None and 0 <= elevation <= 30,
    }
    reasons.extend(name for name, passed in required.items() if not passed)
    observer_rain = finite(seed.get("observerRainRateMmHr"))
    if observer_rain is not None and observer_rain >= 0.1:
        reasons.append("observer_not_dry")
    support = seed.get("spatialSupport") or {}
    neighbors = finite(support.get("adjacentWetCells"))
    if support.get("isolatedPixel") is True or (neighbors is not None and neighbors < 1):
        reasons.append("radar_spatial_support")
    if neighbors is None and support.get("connectedWetCellCount") is None:
        reasons.append("radar_spatial_support_unknown")
    return not reasons, reasons


class TokenScheduler:
    def __init__(self, capacity: int, threshold: float, rate_per_24_hours: float = 10, cooldown_hours: float = 12):
        self.capacity = int(capacity)
        self.threshold = float(threshold)
        self.rate_per_hour = float(rate_per_24_hours) / 24
        self.cooldown_hours = float(cooldown_hours)
        self.tokens = float(capacity)
        self.last_refill: datetime | None = None
        self.last_alert_by_ancestor: dict[str, datetime] = {}

    def refill(self, scan_at: datetime) -> None:
        if self.last_refill is None:
            self.last_refill = scan_at
            return
        elapsed_hours = max(0.0, (scan_at - self.last_refill).total_seconds() / 3600)
        self.tokens = min(float(self.capacity), self.tokens + elapsed_hours * self.rate_per_hour)
        self.last_refill = scan_at

    def most_recent_alert(self, candidate: dict) -> datetime | None:
        times = [
            self.last_alert_by_ancestor[ancestor]
            for ancestor in candidate["ancestorEventIds"]
            if ancestor in self.last_alert_by_ancestor
        ]
        return max(times) if times else None

    def grant(self, scan_at: datetime, candidates: list[dict]) -> list[dict]:
        self.refill(scan_at)
        alerts = []
        ordered = sorted(candidates, key=lambda item: (
            -float(item["score"]),
            -float(item["causalPeakRadar"]),
            -int(item["objectPersistenceScans"]),
            item["eventId"],
        ))
        for candidate in ordered:
            if self.tokens < 1 or not candidate["eligible"] or candidate["score"] < self.threshold:
                continue
            previous = self.most_recent_alert(candidate)
            if previous and (scan_at - previous).total_seconds() < self.cooldown_hours * 3600:
                continue
            self.tokens -= 1
            ancestors = sorted(set(candidate["ancestorEventIds"]) | {candidate["eventId"]})
            for ancestor in ancestors:
                self.last_alert_by_ancestor[ancestor] = scan_at
            alerts.append({
                "capacity": self.capacity,
                "threshold": self.threshold,
                "eventId": candidate["eventId"],
                "ancestorEventIds": ancestors,
                "alertAt": iso_utc(scan_at),
                "score": candidate["score"],
                "isRealertAfterCooldown": previous is not None,
                "tokensRemaining": round(self.tokens, 6),
            })
        return alerts


class StormObjectReplay:
    def __init__(self, config: dict):
        self.config = deepcopy(config)
        eligibility = config["eligibility"]
        capacities = [
            config["scheduler"]["primaryBucketCapacity"],
            *config["scheduler"]["sensitivityBucketCapacities"],
        ]
        self.schedulers = {
            (capacity, float(threshold)): TokenScheduler(
                capacity, threshold,
                rate_per_24_hours=config["scheduler"]["tokensPer24Hours"],
                cooldown_hours=config["scheduler"]["ancestryCooldownHours"],
            )
            for capacity in sorted(set(capacities))
            for threshold in eligibility["scoreThresholds"]
        }
        self.previous_ledger = None
        self.previous_scan_at = None
        self.peak_by_ancestor: dict[str, float] = {}
        self.current_by_ancestor: dict[str, float] = {}

    def _linked_ledger(self, artifact: dict) -> dict:
        scan_at = parse_utc(artifact["observedAt"])
        previous = self.previous_ledger
        if self.previous_scan_at and (scan_at - self.previous_scan_at).total_seconds() > 30 * 60:
            previous = None
        current = {
            "stormObjects": deepcopy(artifact.get("stormObjects") or []),
            "stormLineageEdges": [],
        }
        linked = link_lineage(
            previous, current,
            maximum_motion_cells=int(self.config["stormSegmentation"]["maximumMotionCellsPerScan"]),
            event_key="stormObjects",
            edge_key="stormLineageEdges",
        )
        self.previous_ledger = linked
        self.previous_scan_at = scan_at
        return linked

    def process_artifact(self, artifact: dict) -> dict:
        scan_at = parse_utc(artifact["observedAt"])
        ledger = self._linked_ledger(artifact)
        objects = {item["componentId"]: item for item in ledger["stormObjects"]}
        seeds_by_component: dict[str, list[dict]] = defaultdict(list)
        for attachment in artifact.get("attachedSeeds") or []:
            if attachment["stormComponentId"] in objects:
                seeds_by_component[attachment["stormComponentId"]].append(attachment["seed"])
        candidates = []
        for component_id, seeds in seeds_by_component.items():
            item = objects[component_id]
            ancestors = sorted(set(item.get("ancestorEventIds") or [item["eventId"]]))
            valid = [seed for seed in seeds if valid_seed(seed)[0]]
            if not valid:
                continue
            current = max(float(seed["radarScore"]) for seed in valid)
            inherited_peak = max((self.peak_by_ancestor.get(key, 0.0) for key in ancestors), default=0.0)
            peak = max(inherited_peak, current)
            previous = max((self.current_by_ancestor.get(key, current) for key in ancestors), default=current)
            scans = int(item.get("lineageScanCount") or 1)
            saturation = int(self.config["eligibility"]["persistenceSaturationScans"])
            persistence = min(scans, saturation) / saturation
            trend = max(-1.0, min(1.0, (current - previous) / 20))
            best_seed = None
            best_score = -1.0
            best_sunlight = "unavailable"
            for seed in valid:
                modifier, state = sunlight_modifier(seed)
                score = max(0.0, min(100.0,
                    100 * (
                        0.45 * peak / 100
                        + 0.35 * persistence
                        + 0.15 * float(seed["radarScore"]) / 100
                        + 0.05 * (trend + 1) / 2
                    ) + modifier
                ))
                if score > best_score:
                    best_seed, best_score, best_sunlight = seed, score, state
            for ancestor in ancestors:
                self.peak_by_ancestor[ancestor] = peak
                self.current_by_ancestor[ancestor] = current
            eligible = (
                scans >= int(self.config["eligibility"]["minimumObjectPersistenceScans"])
                and peak >= float(self.config["eligibility"]["minimumCausalPeakRadar"])
                and current >= float(self.config["eligibility"]["minimumCurrentRadar"])
            )
            candidates.append({
                "componentId": component_id,
                "eventId": item["eventId"],
                "ancestorEventIds": ancestors,
                "objectPersistenceScans": scans,
                "causalPeakRadar": round(peak, 3),
                "currentRadar": round(current, 3),
                "score": round(best_score, 3),
                "eligible": eligible,
                "attachedSeedCount": len(seeds),
                "validSeedCount": len(valid),
                "representativeSeed": {
                    key: best_seed.get(key)
                    for key in ("lat", "lon", "rainLat", "rainLon", "radarScore")
                },
                "sunlightState": best_sunlight,
                "envelopeCellCount": item["cellCount"],
                "coreCellCount": item["coreCellCount"],
            })
        # Split siblings can share one event. Keep the best current observer arc
        # for scheduling while retaining every component in the diagnostics.
        by_event = {}
        for candidate in candidates:
            old = by_event.get(candidate["eventId"])
            if old is None or (
                candidate["score"], candidate["causalPeakRadar"], candidate["componentId"]
            ) > (
                old["score"], old["causalPeakRadar"], old["componentId"]
            ):
                by_event[candidate["eventId"]] = candidate
        family_candidates = sorted(by_event.values(), key=lambda item: item["eventId"])
        alerts = []
        for scheduler in self.schedulers.values():
            alerts.extend(scheduler.grant(scan_at, family_candidates))
        edge_counts = defaultdict(int)
        for edge in ledger.get("stormLineageEdges") or []:
            edge_counts[edge["kind"]] += 1
            if edge.get("linkBasis") == "nearest_motion_fallback":
                edge_counts["motionFallback"] += 1
        return {
            "schemaVersion": SCHEMA_VERSION,
            "scanTime": artifact["observedAt"],
            "modelVersion": MODEL_VERSION,
            "schedulerVersion": SCHEDULER_VERSION,
            # Only base-eligible families can cross any frozen score threshold.
            # Counts for the complete candidate set remain in diagnostics.
            "stormFamilies": [
                item for item in family_candidates if item["eligible"]
            ],
            "objectLineage": [
                {
                    "componentId": item["componentId"],
                    "eventId": item["eventId"],
                    "ancestorEventIds": sorted(set(
                        item.get("ancestorEventIds") or [item["eventId"]]
                    )),
                }
                for item in sorted(
                    (objects[component_id] for component_id in seeds_by_component),
                    key=lambda value: value["componentId"],
                )
            ],
            "schedulerAlerts": sorted(alerts, key=lambda item: (
                item["capacity"], item["threshold"], item["eventId"]
            )),
            "diagnostics": {
                "stormObjects": len(objects),
                "objectsWithAttachedSeeds": len(seeds_by_component),
                "familyCandidates": len(family_candidates),
                "eligibleFamilies": sum(item["eligible"] for item in family_candidates),
                "unattachedSeeds": artifact["diagnostics"]["unattachedSeeds"],
                "seedAttachmentCoveragePerObject": artifact["diagnostics"]["seedAttachmentCoveragePerObject"],
                "lineageEdges": dict(sorted(edge_counts.items())),
            },
        }


def load_artifact(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def artifact_paths(root: Path) -> list[Path]:
    paths = list(Path(root).glob("shard-*/*/scan-*.json.gz"))
    return sorted(paths, key=lambda path: path.name)


def content_sha256(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
