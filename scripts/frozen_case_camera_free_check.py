#!/usr/bin/env python
"""Camera-FREE seed check around the three frozen missed-bow cases.

Addresses the reproducibility gap Codex identified: the candidate pool retains
only FAA-camera-matched seeds, so the study addendum's frozen-case numbers were
not recomputable from the package. This script regenerates production seeds
(no camera gate) at 10-minute steps from 90 minutes BEFORE each case window to
30 minutes AFTER it, and retains EVERY seed within 60 km of ANY of the case's
fixture observations, plus per-scan summaries, as a machine-readable artifact.
Case windows and observation points are read from the frozen-case fixture
(validation/opportunity-ledger/case-fixture-v1.json), not hardcoded.

Caveats stated in the artifact: the window truncates lineage history (a storm
seeding before window start shows fewer scans than its true lineage), and
per-scan presence within a fixed radius is not the production lineage
definition — this is a bounded diagnostic, not a causal replay.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from detector_core import observer_seeds_from_rain_grid  # noqa: E402
from mrms_source import download_and_expand, key_for_time, object_from_key, open_precip_grid  # noqa: E402

OUTPUT_DIR = ROOT / "validation" / "frozen-case-pool-check"
CACHE = OUTPUT_DIR / "mrms-cache"
RADIUS_KM = 60.0
WINDOW_BEFORE_MIN = 90
WINDOW_AFTER_MIN = 30

CASE_IDS = ("baltimore-dundalk-20260728", "colorado-multicamera-20260730",
            "utah-ogden-bear-river-20260730")


def load_cases() -> list[dict]:
    fixture = json.loads((ROOT / "validation" / "opportunity-ledger" / "case-fixture-v1.json")
                         .read_text(encoding="utf-8-sig"))
    cases = []
    for case in fixture["cases"]:
        if case["id"] not in CASE_IDS:
            continue
        cases.append({"id": case["id"], "window": case["window"],
                      "observations": [{"id": obs["id"], "lat": obs["lat"], "lon": obs["lon"]}
                                       for obs in case["observations"]]})
    missing = set(CASE_IDS) - {case["id"] for case in cases}
    if missing:
        raise SystemExit(f"fixture missing cases: {missing}")
    return cases


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def distance_km(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * 111,
                      (lon1 - lon2) * 111 * math.cos(math.radians(lat1)))


def main() -> int:
    results = []
    for case in load_cases():
        start = parse_utc(case["window"][0]) - timedelta(minutes=WINDOW_BEFORE_MIN)
        start = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % 10)
        stop = parse_utc(case["window"][1]) + timedelta(minutes=WINDOW_AFTER_MIN)
        scans = []
        cursor = start
        while cursor <= stop:
            entry = {"scanTime": iso(cursor)}
            try:
                obj = object_from_key(key_for_time(cursor))
                path = download_and_expand(obj, CACHE)
                field = open_precip_grid(path)
                lat = field.coords["latitude"].values
                lon = field.coords["longitude"].values.copy()
                lon[lon > 180] -= 360
                seeds = observer_seeds_from_rain_grid(lat, lon, field.values, cursor,
                                                      stride=10, maximum=400)
                near = [seed for seed in seeds
                        if any(distance_km(seed["lat"], seed["lon"], obs["lat"], obs["lon"]) <= RADIUS_KM
                               for obs in case["observations"])]
                entry.update({"mrmsKey": obj.key, "seedsWithinRadius": near,
                              "seedCount": len(near),
                              "bestRadarScore": max((seed["radarScore"] for seed in near), default=None)})
            except Exception as error:
                entry.update({"error": str(error)[:200], "seedCount": 0, "seedsWithinRadius": []})
            scans.append(entry)
            print(f"{case['id']} {entry['scanTime']}: seeds={entry['seedCount']} "
                  f"best={entry.get('bestRadarScore')}", flush=True)
            cursor += timedelta(minutes=10)
        present = [scan for scan in scans if scan["seedCount"]]
        scores = [scan["bestRadarScore"] for scan in present if scan.get("bestRadarScore") is not None]
        results.append({**case, "radiusKm": RADIUS_KM,
                        "windowBeforeMinutes": WINDOW_BEFORE_MIN, "windowAfterMinutes": WINDOW_AFTER_MIN,
                        "scansWithSeeds": len(present), "scansTotal": len(scans),
                        "peakRadarScore": max(scores) if scores else None,
                        "peakRadarAt": max(((scan["bestRadarScore"], scan["scanTime"]) for scan in present
                                            if scan.get("bestRadarScore") is not None), default=(None, None))[1],
                        "scans": scans})
    artifact = {
        "schemaVersion": "frozen-case-camera-free-check.v1",
        "generator": "scripts/frozen_case_camera_free_check.py",
        "seedGenerator": "worker.detector_core.observer_seeds_from_rain_grid(stride=10, maximum=400), NO camera gate",
        "caveats": ["window truncates lineage history",
                    "fixed-radius per-scan presence is a bounded diagnostic, not production lineage or causal replay",
                    "post-cutoff data (Jul 28-30): development-set diagnostic only"],
        "cases": results,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / "camera-free-seeds.json"
    out.write_text(json.dumps(artifact, indent=1) + "\n", encoding="utf-8")
    print("artifact:", out)
    for case in results:
        print(f"{case['id']}: {case['scansWithSeeds']}/{case['scansTotal']} scans, "
              f"peak {case['peakRadarScore']} at {case['peakRadarAt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
