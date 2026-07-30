#!/usr/bin/env python3
"""Scan every QCRAD day in the ARM ASI camera era for surface sunshowers."""

from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"
CAMERA_START = "20230414"
EVENT_GAP_SECONDS = 3600
REVIEWED_WINDOWS = (
    ("2024-05-13T21:30:00Z", "2024-05-13T22:30:00Z"),
    ("2024-06-29T23:30:00Z", "2024-06-30T00:30:00Z"),
    ("2025-07-25T23:45:00Z", "2025-07-26T01:15:00Z"),
)


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


qscan = load_module("qcrad_precip", "arm-qcrad-precip-scan.py")
surface = qscan.surface
fast = qscan.fast


def cluster(minutes):
    if not minutes:
        return []
    minutes.sort(key=lambda item: item["observedAt"])
    groups = [[minutes[0]]]
    for item in minutes[1:]:
        previous = datetime.fromisoformat(groups[-1][-1]["observedAt"].replace("Z", "+00:00"))
        current = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        if item["day"] == groups[-1][-1]["day"] and (current - previous).total_seconds() <= EVENT_GAP_SECONDS:
            groups[-1].append(item)
        else:
            groups.append([item])
    events = []
    for group in groups:
        best = max(group, key=lambda item: item["score"])
        events.append({
            **best,
            "eventStart": group[0]["observedAt"],
            "eventEnd": group[-1]["observedAt"],
            "matchingMinutes": len(group),
        })
    return events


def main():
    session = requests.Session()
    session.headers["User-Agent"] = "rainbow-connector-validation/1.0"
    info = fast.file_index(surface.QCRAD, session)
    days = sorted(day for day in info if day >= CAMERA_START)
    auth = surface.credentials()
    minutes, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(qscan.process, day, info, auth): day for day in days}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                minutes.extend(future.result())
            except Exception as error:
                errors.append({"day": futures[future], "error": str(error)})
            if number % 50 == 0 or number == len(days):
                print(f"Scanned {number}/{len(days)} QCRAD days", flush=True)
    events = cluster(minutes)
    events.sort(key=lambda item: item["score"], reverse=True)
    for event in events:
        event["likelyLiquidPrecipitation"] = event["airTemperatureC"] >= 5
        event["previouslyReviewed"] = any(
            event["eventStart"] <= end and event["eventEnd"] >= start
            for start, end in REVIEWED_WINDOWS
        )
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Complete-camera-era surface sunshower discovery using QCRAD direct sun and precipitation.",
        "cameraStart": CAMERA_START,
        "daysAvailable": len(days),
        "errors": errors,
        "matchingMinutes": len(minutes),
        "events": events,
        "newEvents": [
            event for event in events
            if not event["previouslyReviewed"] and event["likelyLiquidPrecipitation"]
        ],
    }
    output = VALIDATION / "surface-sunshower-all-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    for number, event in enumerate(payload["newEvents"][:40], 1):
        print(number, event["observedAt"], event["score"], event["matchingMinutes"], event["measuredDirectNormalIrradianceWm2"], event["measuredPrecipitationMm"])


if __name__ == "__main__":
    main()
