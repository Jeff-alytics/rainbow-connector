"""Standard regression report for Opportunity Ledger observer cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from opportunity_ledger import swath_contains


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _distance(left_lat: float, left_lon: float, right_lat: float, right_lon: float) -> float:
    north = (left_lat - right_lat) * 111
    east = (left_lon - right_lon) * 111 * math.cos(math.radians(left_lat))
    return math.hypot(north, east)


def _case_ledgers(case: dict, ledgers: list[dict]) -> list[dict]:
    start, end = case.get("window") or [None, None]
    if not start or not end:
        return []
    first, last = _time(start), _time(end)
    return [ledger for ledger in ledgers if first <= _time(ledger["scanTime"]) <= last]


def observation_result(observation: dict, ledgers: list[dict]) -> dict:
    matched_events = set()
    inside = False
    nearest_swath = None
    nearest_representative = None
    for ledger in ledgers:
        for opportunity in ledger.get("opportunities") or []:
            swath = opportunity.get("observerSwath") or {}
            contains, swath_distance = swath_contains(swath, observation["lat"], observation["lon"])
            if swath_distance is not None and (nearest_swath is None or swath_distance < nearest_swath):
                nearest_swath = swath_distance
            if contains:
                inside = True
                matched_events.add(opportunity["eventId"])
            for point in swath.get("representativeCandidates") or []:
                distance = _distance(observation["lat"], observation["lon"], point["lat"], point["lon"])
                if nearest_representative is None or distance < nearest_representative:
                    nearest_representative = distance
    failures = []
    if ledgers and not inside:
        failures.append("OBSERVER_SWATH_MISS")
    if inside and (nearest_representative is None or nearest_representative > 15):
        failures.append("REPRESENTATIVE_SEED_MISS")
    return {
        "observationId": observation["id"], "label": observation.get("label"),
        "observer": {"lat": observation["lat"], "lon": observation["lon"], "uncertaintyKm": observation.get("uncertaintyKm")},
        "insideObserverSwath": inside,
        "nearestSwathKm": None if nearest_swath is None else round(nearest_swath, 2),
        "nearestRepresentativeKm": None if nearest_representative is None else round(nearest_representative, 2),
        "matchedEventIds": sorted(matched_events), "failureCodes": failures,
    }


def build_report(fixture: dict, ledgers: list[dict]) -> dict:
    rows = []
    for case in fixture.get("cases") or []:
        available = _case_ledgers(case, ledgers)
        if case.get("status") != "replay_ready":
            disposition = "fixture_not_ready"
        elif not available:
            disposition = "ledger_window_missing"
        else:
            disposition = "evaluated"
        observations = [observation_result(item, available) for item in case.get("observations") or []]
        rows.append({
            "caseId": case["id"], "disposition": disposition,
            "ledgerScans": len(available),
            "rainEventRetained": any(ledger.get("rainEvents") for ledger in available),
            "opportunityRetained": any(ledger.get("opportunities") for ledger in available),
            "observations": observations,
        })
    core = {
        "schemaVersion": "opportunity-ledger-replay.v1",
        "fixtureContentSha256": fixture.get("contentSha256"),
        "ledgerMethodVersions": sorted({ledger.get("methodVersion") for ledger in ledgers if ledger.get("methodVersion")}),
        "cases": rows,
        "summary": {
            "cases": len(rows), "evaluated": sum(row["disposition"] == "evaluated" for row in rows),
            "missingLedgerWindows": sum(row["disposition"] == "ledger_window_missing" for row in rows),
            "fixturesAwaitingTime": sum(row["disposition"] == "fixture_not_ready" for row in rows),
            "observationsInsideSwath": sum(item["insideObserverSwath"] for row in rows for item in row["observations"]),
        },
    }
    core["contentSha256"] = hashlib.sha256(json.dumps(core, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    return core


def load_ledgers(directory: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="validation/opportunity-ledger/case-fixture-v1.json")
    parser.add_argument("--ledger-dir", required=True, nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args()
    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    ledgers = [ledger for directory in args.ledger_dir for ledger in load_ledgers(Path(directory))]
    report = build_report(fixture, ledgers)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
