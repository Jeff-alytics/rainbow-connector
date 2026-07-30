#!/usr/bin/env python3
"""Rerun the corrected ENA scan and select an unseen empirical holdout batch."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
RANKED = ROOT / "validation" / "arm-ena" / "surface-sunshower-ranked.json"
OUTPUT = ROOT / "validation" / "arm-ena" / "targeted-batch-01.json"


def range_score(value, low, high, weight, scale):
    if low <= value <= high:
        return weight
    distance = low - value if value < low else value - high
    return max(-weight, weight - distance / scale * weight)


def main():
    old = json.loads(RANKED.read_text(encoding="utf-8"))["events"]
    reviewed = {event["observedAt"] for event in old[:60]}
    subprocess.run(
        ["uv", "run", "python", str(ROOT / "scripts" / "arm-ena-sunshower-scan.py")],
        cwd=ROOT,
        check=True,
    )
    corrected = json.loads(RANKED.read_text(encoding="utf-8"))["events"]
    eligible = [event for event in corrected if event["observedAt"] not in reviewed]
    for event in eligible:
        event["targetingScore"] = round(
            range_score(event["expectedRainbowTopElevationDeg"], 17, 33, 60, 8)
            + range_score(event["measuredDirectNormalIrradianceWm2"], 200, 650, 25, 250)
            + range_score(math.log10(max(event["measuredRainRateMmHr"], 0.01)), math.log10(0.15), math.log10(7), 25, 1)
            + range_score(event["relativeHumidityPct"], 65, 90, 20, 25)
            + min(event["matchingMinutes"], 3) * 3,
            2,
        )
    eligible.sort(key=lambda event: event["targetingScore"], reverse=True)
    selected = eligible[:20]
    payload = {
        "schemaVersion": 1,
        "purpose": "Unseen holdout cases ranked from empirical ranges learned from six ENA positives.",
        "reviewedTimestampCount": len(reviewed),
        "eligibleUnseenEvents": len(eligible),
        "events": selected,
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT)
    print(f"Eligible unseen events: {len(eligible)}")
    for number, event in enumerate(selected, 1):
        print(number, event["observedAt"], event["targetingScore"], event["expectedRainbowTopElevationDeg"])


if __name__ == "__main__":
    main()
