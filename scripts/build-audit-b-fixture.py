#!/usr/bin/env python
"""Build the frozen Audit B positive-event regression fixture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "validation" / "audit-b"
OUTPUT = AUDIT_DIR / "regression-fixture-v1.json"


def read_json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def frame_window(frames: list[dict], fallback: str) -> tuple[str, str]:
    times = sorted(frame["observedAt"] for frame in frames if frame.get("observedAt"))
    return (times[0], times[-1]) if times else (fallback, fallback)


def faa_event(candidate_id: str, item: dict, source_file: str, scope: str) -> dict:
    site, camera = item.get("site") or {}, item.get("camera") or {}
    lat, lon = item.get("latitude", site.get("latitude")), item.get("longitude", site.get("longitude"))
    name, state = item.get("siteName", site.get("siteName")), item.get("state", site.get("state"))
    bearing = item.get("cameraBearing", camera.get("cameraBearing"))
    difference = item.get("cameraDifference")
    if difference is None and bearing is not None and item.get("antiSolarAzimuth") is not None:
        difference = abs((bearing - item["antiSolarAzimuth"] + 180) % 360 - 180)
    start, end = frame_window(item.get("frames") or [], item["observedAt"])
    return {
        "id": f"faa-{candidate_id}", "label": "rainbow", "evidenceClass": "camera_confirmed",
        "productionScope": scope, "eventGroup": f"faa-{candidate_id}",
        "observer": {"lat": lat, "lon": lon, "uncertaintyKm": 0.2, "basis": "fixed FAA camera"},
        "observation": {"representativeAt": item["observedAt"], "windowStart": start, "windowEnd": end, "precision": "one_or_more_frames_in_window"},
        "provenance": {"kind": "human_camera_review", "sourceId": candidate_id, "sourceFile": source_file, "camera": f"FAA WeatherCam {name}, {state}"},
        "frozenEvidence": {"sunElevationDeg": item.get("sunElevation"), "antiSolarAzimuthDeg": item.get("antiSolarAzimuth"), "cameraBearingDeg": bearing, "cameraDifferenceDeg": round(difference, 2) if difference is not None else None, "rainInches": item.get("rainInches"), "weather": item.get("weather") or None, "rankingScore": item.get("finalScore", item.get("score"))},
        "expected": {"rainbowPossible": True, "recallProbe": scope == "conus_recall"},
    }


def build_faa_events() -> list[dict]:
    events = []
    ranked = read_json("validation/faa/faa-ranked-pilot.json")
    for index in (0, 2, 5, 6):
        events.append(faa_event(f"faa-{index + 1:02d}", ranked[index], "validation/faa/faa-ranked-pilot.json", "conus_recall"))
    expansion = read_json("validation/faa/expansion-review/faa-review-batch-02.json")
    for item in expansion:
        if item["candidateId"] in {"faax-01", "faax-02", "faax-03", "faax-04"}:
            events.append(faa_event(item["candidateId"], item, "validation/faa/expansion-review/faa-review-batch-02.json", "conus_recall"))
    network = read_json("validation/faa/network-expansion-review/faa-review-batch-03.json")
    scopes = {"faan-01": "conus_recall", "faan-02": "science_only", "faan-04": "science_only"}
    for item in network:
        if item["candidateId"] in scopes:
            events.append(faa_event(item["candidateId"], item, "validation/faa/network-expansion-review/faa-review-batch-03.json", scopes[item["candidateId"]]))
    return events


def build_arm_events() -> list[dict]:
    manifest = read_json("validation/arm-lamont/manifest.json")
    pairs, site = {pair["id"]: pair for pair in manifest["pairs"]}, manifest["site"]
    labels = [("surface-02-fullres", "lamont-039", "2024-06-30T00:05:00Z"), ("surface-03-fullres", "lamont-012", "2025-07-26T00:20:00Z")]
    events = []
    for candidate_id, pair_id, center in labels:
        source = pairs[pair_id]["event"]
        events.append({
            "id": f"arm-{pair_id}", "label": "rainbow", "evidenceClass": "camera_confirmed", "productionScope": "conus_recall", "eventGroup": f"arm-{pair_id}",
            "observer": {"lat": site["lat"], "lon": site["lon"], "uncertaintyKm": 0.1, "basis": "fixed ARM SGP camera"},
            "observation": {"representativeAt": center, "windowStart": center, "windowEnd": center, "precision": "human-reviewed-five-frame-sequence"},
            "provenance": {"kind": "human_camera_review", "sourceId": candidate_id, "sourceFile": "validation/arm-lamont/human-labels.json", "camera": site["name"]},
            "frozenEvidence": {"sunElevationDeg": source.get("sunElevationDeg"), "antiSolarAzimuthDeg": source.get("antiSolarBearingDeg"), "directNormalIrradianceWm2": source.get("directNormalIrradianceWm2"), "cloudCoverPct": source.get("cloudCoverPct"), "precipitationMm": source.get("precipitationMm")},
            "expected": {"rainbowPossible": True, "recallProbe": True},
        })
    return events


def canonical_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def main() -> int:
    manual = read_json("validation/audit-b/manual-probes-v1.json")
    events = [*build_faa_events(), *build_arm_events(), *manual["events"]]
    events.sort(key=lambda event: (event["observation"].get("representativeAt") or event["observation"]["windowEnd"], event["id"]))
    if len({event["id"] for event in events}) != len(events):
        raise RuntimeError("Audit B event IDs must be unique")
    fixture = {
        "schemaVersion": 1, "fixtureId": "rainbow-positive-regression-v1", "createdAt": "2026-07-29T15:00:00Z",
        "purpose": "Frozen positive-event recall fixture. It is not a precision dataset and intentionally contains no negative controls.",
        "rules": {"astronomicalHorizonDeg": -0.833, "physicalBowSunMaximumDeg": 42, "currentWorkerSeedSunRangeDeg": [0, 30], "currentStrictGoSunRangeDeg": [5, 22], "currentPossibleSunRangeDeg": [0, 30], "currentDirectGoMinWm2": 200, "currentDirectPossibleMinWm2": 120},
        "liveScanBaseline": {"generatedAt": "2026-07-29T14:20:25.435629Z", "goCandidates": 1, "possibleCandidates": 38, "errors": 0, "dniErrors": 0, "runtimeMs": 15625},
        "events": events,
    }
    fixture["contentSha256"] = canonical_hash({k: v for k, v in fixture.items() if k != "contentSha256"})
    OUTPUT.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    conus = [event for event in events if event["productionScope"] == "conus_recall"]
    print(json.dumps({"output": str(OUTPUT), "events": len(events), "conusRecall": len(conus), "conusCameraConfirmed": sum(event["evidenceClass"] == "camera_confirmed" for event in conus), "socialReports": sum(event["evidenceClass"] == "social_report" for event in events), "sha256": fixture["contentSha256"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
