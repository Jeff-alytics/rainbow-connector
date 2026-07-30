"""Audit B recall analysis over the frozen confirmed-positive fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from detector_core import solar_position

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "validation" / "audit-b" / "regression-fixture-v1.json"
DEFAULT_GOES = ROOT / "validation" / "audit-b" / "historical-goes-v1.json"
DEFAULT_HISTORY = ROOT / "validation" / "audit-b" / "production-history-replay-v1.json"
DEFAULT_REPORT = ROOT / "validation" / "audit-b" / "audit-b-report-v1.json"


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def reference_time(event: dict) -> datetime:
    observation = event["observation"]
    exact = observation.get("confirmedFrameAt") or observation.get("representativeAt")
    if exact:
        return parse_utc(exact)
    start, end = parse_utc(observation["windowStart"]), parse_utc(observation["windowEnd"])
    return start + (end - start) / 2


def time_samples(event: dict, step_minutes: int = 5) -> list[datetime]:
    observation = event["observation"]
    start, end = parse_utc(observation["windowStart"]), parse_utc(observation["windowEnd"])
    if end <= start:
        return [reference_time(event)]
    values = [start]
    cursor = start + timedelta(minutes=step_minutes)
    while cursor < end:
        values.append(cursor)
        cursor += timedelta(minutes=step_minutes)
    values.append(end)
    return values


def within(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def canonical_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def goes_index(payload: dict | None) -> dict[str, dict]:
    return {row["eventId"]: row for row in (payload or {}).get("events", [])}


def analyze_event(event: dict, historical_goes: dict[str, dict], production_history: dict[str, dict] | None = None) -> dict:
    observer = event["observer"]
    if not isinstance(observer.get("lat"), (int, float)) or not isinstance(observer.get("lon"), (int, float)):
        return {
            "id": event["id"], "eventGroup": event["eventGroup"], "evidenceClass": event["evidenceClass"],
            "productionScope": event["productionScope"], "pointScorable": False,
            "reason": "no defensible point location", "currentPolicyReplay": "not_scorable",
        }
    lat, lon = float(observer["lat"]), float(observer["lon"])
    ref = reference_time(event)
    reference_elevation, reference_bearing = solar_position(ref, lat, lon)
    samples = [(when, solar_position(when, lat, lon)[0]) for when in time_samples(event)]
    elevations = [elevation for _, elevation in samples]
    any_physical = any(-0.833 <= elevation <= 42 for elevation in elevations)
    any_worker = any(0 <= elevation <= 30 for elevation in elevations)
    any_strict = any(5 <= elevation <= 22 for elevation in elevations)
    risks = []
    if any_physical and not any_worker:
        risks.append("current_0_to_30_degree_seed_window_misses_entire_observation_window")
    if any_worker and not any_strict:
        risks.append("outside_strict_go_5_to_22_degree_band")
    frozen = event.get("frozenEvidence") or {}
    dni = frozen.get("directNormalIrradianceWm2")
    if isinstance(dni, (int, float)) and dni < 120:
        risks.append("frozen_dni_below_possible_threshold")
    if event["provenance"]["kind"] == "production_review":
        replay = f"observed_in_production_as_{str(frozen.get('candidateClass') or 'unknown').lower()}"
    else:
        replay = "requires_archived_mrms_rain_geometry"
    goes = historical_goes.get(event["id"])
    frozen_elevation = frozen.get("sunElevationDeg")
    return {
        "id": event["id"], "eventGroup": event["eventGroup"], "evidenceClass": event["evidenceClass"],
        "productionScope": event["productionScope"], "pointScorable": True,
        "referenceAt": iso_utc(ref),
        "solar": {
            "referenceElevationDeg": round(reference_elevation, 2),
            "referenceAzimuthDeg": round(reference_bearing, 2),
            "windowMinElevationDeg": round(min(elevations), 2),
            "windowMaxElevationDeg": round(max(elevations), 2),
            "frozenElevationDeg": frozen_elevation,
            "frozenDifferenceDeg": round(reference_elevation - frozen_elevation, 2) if isinstance(frozen_elevation, (int, float)) else None,
            "physicalBowWindowPresent": any_physical,
            "currentWorkerSeedWindowPresent": any_worker,
            "strictGoSunWindowPresent": any_strict,
        },
        "rainDistanceKm": frozen.get("rainDistanceKm"),
        "directNormalIrradianceWm2": dni,
        "frozenGoesDecision": frozen.get("goesDecision"),
        "historicalGoes": goes,
        "productionHistory": (production_history or {}).get(event["id"]),
        "currentPolicyReplay": replay,
        "recallRisks": risks,
    }


def summarize(rows: list[dict], fixture: dict) -> dict:
    conus = [row for row in rows if row["productionScope"] == "conus_recall" and row["pointScorable"]]
    camera = [row for row in conus if row["evidenceClass"] == "camera_confirmed"]
    social = [row for row in conus if row["evidenceClass"] == "social_report"]
    with_goes = [row for row in camera if row.get("historicalGoes")]
    acmc = [row["historicalGoes"].get("goesAcmc") for row in with_goes]
    acmc_available = [source for source in acmc if source and source.get("available")]
    acmc_values = [source for source in acmc_available if source.get("value") in (0, 1)]
    dsrf = [row["historicalGoes"].get("goesDsrf") for row in with_goes]
    dsrf_available = [source for source in dsrf if source and source.get("available")]
    dsrf_values = [source for source in dsrf_available if isinstance(source.get("value"), (int, float))]
    satellite_pair_go = 0
    for row in with_goes:
        sources = row["historicalGoes"]
        cloud, irradiance = sources.get("goesAcmc") or {}, sources.get("goesDsrf") or {}
        if cloud.get("value") == 0 and isinstance(irradiance.get("value"), (int, float)) and irradiance["value"] >= 200:
            satellite_pair_go += 1
    distances = [row["rainDistanceKm"] for row in camera if isinstance(row.get("rainDistanceKm"), (int, float))]
    social_history = [row.get("productionHistory") for row in social if row.get("productionHistory")]
    return {
        "fixtureEvents": len(fixture["events"]),
        "pointScorableConusRecallEvents": len(conus),
        "uniqueConusRecallEventGroups": len({row["eventGroup"] for row in conus}),
        "conusCameraConfirmedEvents": len(camera),
        "conusSocialReportEvents": len(social),
        "physicalBowWindowRetained": sum(row["solar"]["physicalBowWindowPresent"] for row in conus),
        "currentWorker0To30WindowRetained": sum(row["solar"]["currentWorkerSeedWindowPresent"] for row in conus),
        "strictGo5To22WindowRetained": sum(row["solar"]["strictGoSunWindowPresent"] for row in conus),
        "eventsOnlyIn30To42Band": sum(row["solar"]["physicalBowWindowPresent"] and not row["solar"]["currentWorkerSeedWindowPresent"] for row in conus),
        "historicalGoesCameraEventsSampled": len(with_goes),
        "acmcClear": sum(source.get("value") == 0 for source in acmc_values),
        "acmcCloudy": sum(source.get("value") == 1 for source in acmc_values),
        "acmcMissingPixel": len(acmc_available) - len(acmc_values),
        "acmcUnavailable": len(camera) - len(acmc_available),
        "dsrfAtOrAbove200": sum(source["value"] >= 200 for source in dsrf_values),
        "dsrfBelow200": sum(source["value"] < 200 for source in dsrf_values),
        "dsrfMissingPixel": len(dsrf_available) - len(dsrf_values),
        "dsrfUnavailable": len(camera) - len(dsrf_available),
        "currentSatellitePairGo": satellite_pair_go,
        "currentSatellitePairNotGo": len(camera) - satellite_pair_go,
        "rainDistanceKnown": len(distances),
        "rainDistanceUnknown": len(camera) - len(distances),
        "knownRainDistancesKm": distances,
        "socialEventsReplayedAgainstHistory": len(social_history),
        "socialEventsWithCandidateWithin15Km": sum(row.get("candidateWithin15Km") is True for row in social_history),
        "socialEventsWithCandidateWithin40Km": sum(row.get("candidateWithin40Km") is True for row in social_history),
    }


def run(fixture_path: Path = DEFAULT_FIXTURE, goes_path: Path = DEFAULT_GOES, history_path: Path = DEFAULT_HISTORY) -> dict:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected_hash = fixture.get("contentSha256")
    actual_hash = canonical_hash({k: v for k, v in fixture.items() if k != "contentSha256"})
    if expected_hash != actual_hash:
        raise ValueError(f"Fixture hash mismatch: expected {expected_hash}, got {actual_hash}")
    historical = json.loads(goes_path.read_text(encoding="utf-8")) if goes_path.exists() else None
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else None
    history_by_id = {row["eventId"]: row for row in (history or {}).get("events", [])}
    rows = [analyze_event(event, goes_index(historical), history_by_id) for event in fixture["events"]]
    report = {
        "schemaVersion": 1, "auditId": "audit-b-v1", "fixtureId": fixture["fixtureId"],
        "fixtureContentSha256": expected_hash, "historicalGoesPresent": historical is not None,
        "productionHistoryReplayPresent": history is not None,
        "summary": summarize(rows, fixture), "events": rows,
        "limitations": [
            "This is a positive-event recall audit; it cannot estimate false-positive rate or precision.",
            "Archived MRMS grids are not yet frozen, so non-production events cannot be fully replayed through rain-edge sampling.",
            "Social-report windows and named-place coordinates carry the uncertainty recorded in the fixture.",
            "Rain distance is known only for production detections until the MRMS rain-footprint sidecar exists.",
        ],
    }
    report["contentSha256"] = canonical_hash({k: v for k, v in report.items() if k != "contentSha256"})
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--goes", type=Path, default=DEFAULT_GOES)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = run(args.fixture, args.goes, args.history)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **report["summary"], "sha256": report["contentSha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
