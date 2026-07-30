#!/usr/bin/env python3
"""Re-rank radar candidates for sunlit, localized rainbow conditions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"


def opportunity(candidate):
    event = candidate["event"]
    radar = candidate["bestRadar"]
    observer = radar["observerDbz"]
    peak = radar["nearMaxDbz"] or -99
    wet = radar["nearWetPoints"]
    moderate = radar["nearModeratePoints"]
    dni = event["directNormalIrradianceWm2"]
    cloud = event["cloudCoverPct"]
    eligible = (
        radar["score"] > -999
        and (observer is None or observer < 5)
        and 15 <= peak <= 48
        and 1 <= wet <= 16
        and 350 <= dni
        and 5 <= cloud <= 90
    )
    if not eligible:
        return None
    sunlight = min(1, max(0, (dni - 350) / 300)) * 30
    shower_intensity = max(0, 1 - abs(peak - 30) / 18) * 25
    localized_coverage = max(0, 1 - abs(wet - 6) / 10) * 20
    mixed_sky = max(0, 1 - abs(cloud - 50) / 45) * 15
    useful_rain = min(moderate, 5) * 1.5
    return round(sunlight + shower_intensity + localized_coverage + mixed_sky + useful_rain, 3)


def main():
    source = json.loads((VALIDATION / "radar-window-ranked.json").read_text(encoding="utf-8"))
    labels = json.loads((VALIDATION / "human-labels.json").read_text(encoding="utf-8"))
    reviewed = {item["pairId"] for group in (labels.get("labels", []), labels.get("screenLabels", [])) for item in group}
    ranked = []
    for candidate in source["candidates"]:
        score = opportunity(candidate)
        if score is None or candidate["pairId"] in reviewed:
            continue
        ranked.append({
            "pairId": candidate["pairId"],
            "centerTime": candidate["bestRadar"]["radarAt"],
            "opportunityScore": score,
            "inputs": {
                "dniWm2": candidate["event"]["directNormalIrradianceWm2"],
                "cloudCoverPct": candidate["event"]["cloudCoverPct"],
                "observerDbz": candidate["bestRadar"]["observerDbz"],
                "antiSolarPeakDbz": candidate["bestRadar"]["nearMaxDbz"],
                "antiSolarWetPoints": candidate["bestRadar"]["nearWetPoints"],
                "antiSolarModeratePoints": candidate["bestRadar"]["nearModeratePoints"],
            },
        })
    ranked.sort(key=lambda item: item["opportunityScore"], reverse=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Human-review prioritization; heuristic score, not calibrated probability.",
        "selectionRules": {
            "cameraMustBeDryDbz": "<5 or no echo",
            "antiSolarPeakDbz": "15-48",
            "antiSolarWetPointCount": "1-16",
            "minimumDniWm2": 350,
            "cloudCoverPct": "5-90",
        },
        "candidates": ranked,
    }
    output = VALIDATION / "rainbow-opportunity-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    for index, item in enumerate(ranked[:10], 1):
        print(index, item["pairId"], item["centerTime"], item["opportunityScore"])


if __name__ == "__main__":
    main()
