#!/usr/bin/env python3
"""Scan QCRAD for simultaneous measured direct sun and surface precipitation."""

from __future__ import annotations

import importlib.util
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


surface = module("surface_scan", "arm-surface-sunshower-scan.py")
fast = module("surface_fast", "arm-surface-sunshower-fast.py")


def scan(path):
    matches = []
    with xr.open_dataset(path) as dataset:
        dni = np.asarray(dataset.short_direct_normal.values, dtype=float)
        rain = np.asarray(dataset.precip.values, dtype=float)
        air_temperature = np.asarray(dataset.Temp_Air.values, dtype=float)
        relative_humidity = np.asarray(dataset.rh.values, dtype=float)
        times = dataset.time.values.astype("datetime64[s]")
        for index in np.where((dni >= 100) & (rain > 0))[0]:
            stamp = np.datetime_as_string(times[index], unit="s") + "Z"
            elevation = surface.solar_elevation(stamp)
            if not 0 < elevation < 42 or not math.isfinite(dni[index]) or not math.isfinite(rain[index]):
                continue
            rain_rate_equivalent = float(rain[index]) * 60
            rain_score = max(0, 1 - abs(math.log10(max(rain_rate_equivalent, 0.1)) - math.log10(1.5)) / 1.5) * 30
            matches.append({
                "observedAt": stamp,
                "measuredDirectNormalIrradianceWm2": round(float(dni[index]), 2),
                "measuredPrecipitationMm": round(float(rain[index]), 3),
                "airTemperatureC": round(float(air_temperature[index]), 2),
                "relativeHumidityPct": round(float(relative_humidity[index]), 2),
                "sunElevationDeg": round(elevation, 2),
                "score": round(min(float(dni[index]), 900) / 15 + rain_score, 3),
            })
    return matches


def process(day, info, auth):
    path = fast.download(info[day], surface.QCRAD, auth)
    return [{"day": day, **item} for item in scan(path)]


def main():
    manifest = json.loads((VALIDATION / "manifest.json").read_text(encoding="utf-8"))
    wanted = {pair["event"]["observedAt"][:10].replace("-", "") for pair in manifest["pairs"]}
    session = requests.Session()
    session.headers["User-Agent"] = "rainbow-connector-validation/1.0"
    info = fast.file_index(surface.QCRAD, session)
    days = sorted(wanted & info.keys())
    auth = surface.credentials()
    candidates, errors = [], []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(process, day, info, auth): day for day in days}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                candidates.extend(future.result())
            except Exception as error:
                errors.append({"day": futures[future], "error": str(error)})
            if number % 20 == 0 or number == len(days):
                print(f"Scanned {number}/{len(days)} QCRAD days", flush=True)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Measured surface sunshower discovery using QCRAD direct-normal irradiance and precipitation.",
        "daysRequested": len(wanted),
        "daysAvailable": len(days),
        "errors": errors,
        "candidates": candidates,
    }
    output = VALIDATION / "surface-sunshower-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    for number, item in enumerate(candidates[:30], 1):
        print(number, item["observedAt"], item["score"], item["measuredDirectNormalIrradianceWm2"], item["measuredPrecipitationMm"])


if __name__ == "__main__":
    main()
