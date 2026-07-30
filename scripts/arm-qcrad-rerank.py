#!/usr/bin/env python3
"""Rank rainbow candidates with measured one-minute ARM direct irradiance."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"
QCRAD_STREAM = "sgpqcradbrs1longC1.c1"
INDEX_URL = "https://adc.arm.gov/elastic/file_info/_search"
DOWNLOAD_URL = "https://adc.arm.gov/armlive/saveData"


def credentials():
    values = {}
    for line in (ROOT / ".env.arm.local").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return f'{values["ARM_USER_ID"]}:{values["ARM_ACCESS_TOKEN"]}'


def qcrad_file(day: str, auth: str, session: requests.Session):
    directory = VALIDATION / "qcrad"
    directory.mkdir(parents=True, exist_ok=True)
    response = session.get(INDEX_URL, params={"q": f"file_name.keyword:{QCRAD_STREAM}.{day}*", "size": 2}, timeout=30)
    response.raise_for_status()
    hits = response.json().get("hits", {}).get("hits", [])
    if not hits:
        return None
    info = hits[0]["_source"]
    destination = directory / info["file_name"]
    if not destination.exists() or destination.stat().st_size != info["file_size"]:
        with session.get(DOWNLOAD_URL, params={"user": auth, "file": info["file_name"]}, stream=True, timeout=90) as download:
            download.raise_for_status()
            with destination.open("wb") as output:
                for chunk in download.iter_content(1024 * 128):
                    output.write(chunk)
    return destination


def direct_normal_variable(dataset: xr.Dataset):
    matches = []
    for name, variable in dataset.data_vars.items():
        description = " ".join(str(variable.attrs.get(key, "")) for key in ("long_name", "standard_name"))
        text = f"{name} {description}".lower().replace("_", " ")
        if "direct normal" in text and "qc" not in name.lower() and "time" in variable.dims:
            matches.append(name)
    if not matches:
        raise RuntimeError("No direct-normal irradiance variable found in QCRAD file")
    return sorted(matches, key=lambda name: ("short" not in name.lower(), len(name)))[0]


def load_day(path: Path):
    with xr.open_dataset(path) as dataset:
        variable = direct_normal_variable(dataset)
        return dataset[[variable]].load(), variable


def measured_dni(dataset: xr.Dataset, variable: str, timestamp: str):
    target = np.datetime64(timestamp.replace("Z", "")).astype("datetime64[ns]")
    times = dataset.time.values.astype("datetime64[ns]")
    index = int(np.argmin(np.abs(times - target)))
    separation = abs((times[index] - target) / np.timedelta64(1, "s"))
    value = float(dataset[variable].isel(time=index).values)
    return value if separation <= 90 and math.isfinite(value) and value >= 0 else None


def radar_geometry(snapshot):
    observer = snapshot["observerDbz"]
    peak = snapshot["nearMaxDbz"]
    wet = snapshot["nearWetPoints"]
    moderate = snapshot["nearModeratePoints"]
    return (
        snapshot["score"] > -999
        and (observer is None or observer < 15)
        and peak is not None and 5 <= peak <= 60
        and 1 <= wet <= 28
        and moderate <= 20
    )


def score(snapshot, dni):
    peak = snapshot["nearMaxDbz"]
    wet = snapshot["nearWetPoints"]
    moderate = snapshot["nearModeratePoints"]
    sunlight = min(40, max(0, (dni - 250) / 15))
    intensity = max(0, 1 - abs(peak - 28) / 20) * 25
    coverage = max(0, 1 - abs(wet - 6) / 10) * 20
    useful_rain = min(moderate, 4) * 2
    observer_penalty = max(0, snapshot["observerDbz"] or 0) * 1.5
    return round(sunlight + intensity + coverage + useful_rain - observer_penalty, 3)


def main():
    radar = json.loads((VALIDATION / "radar-window-ranked.json").read_text(encoding="utf-8"))
    labels = json.loads((VALIDATION / "human-labels.json").read_text(encoding="utf-8"))
    reviewed = {item["pairId"] for group in (labels.get("labels", []), labels.get("screenLabels", [])) for item in group}
    reviewed.update({"lamont-001", "lamont-004", "lamont-007", "lamont-017", "lamont-019"})
    auth = credentials()
    session = requests.Session()
    session.headers["User-Agent"] = "rainbow-connector-validation/1.0"
    days = {}
    ranked = []
    for candidate in radar["candidates"]:
        if candidate["pairId"] in reviewed:
            continue
        best = None
        for snapshot in candidate["radarWindow"]:
            if not radar_geometry(snapshot):
                continue
            day = snapshot["radarAt"][:10].replace("-", "")
            if day not in days:
                path = qcrad_file(day, auth, session)
                days[day] = load_day(path) if path else None
            if not days[day]:
                continue
            dataset, variable = days[day]
            dni = measured_dni(dataset, variable, snapshot["radarAt"])
            if dni is None or dni < 100:
                continue
            item = {
                "pairId": candidate["pairId"],
                "centerTime": snapshot["radarAt"],
                "opportunityScore": score(snapshot, dni),
                "measuredDirectNormalIrradianceWm2": round(dni, 2),
                "observerDbz": snapshot["observerDbz"],
                "antiSolarPeakDbz": snapshot["nearMaxDbz"],
                "antiSolarWetPoints": snapshot["nearWetPoints"],
                "antiSolarModeratePoints": snapshot["nearModeratePoints"],
            }
            if best is None or item["opportunityScore"] > best["opportunityScore"]:
                best = item
        if best:
            ranked.append(best)
    ranked.sort(key=lambda item: item["opportunityScore"], reverse=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Human-review prioritization using measured 1-minute QCRAD DNI; not calibrated probability.",
        "qcradStream": QCRAD_STREAM,
        "candidates": ranked,
    }
    output = VALIDATION / "qcrad-rainbow-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    for index, item in enumerate(ranked[:20], 1):
        print(index, item["pairId"], item["centerTime"], item["opportunityScore"], item["measuredDirectNormalIrradianceWm2"])


if __name__ == "__main__":
    main()
