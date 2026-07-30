#!/usr/bin/env python3
"""Find reviewable surface-sunshower events in the ARM ENA ASI era."""
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

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "arm-ena"
QCRAD = "enaqcrad1longC1.c1"
MET = "enametC1.b1"
ASI = "enaasiskyimageC1.a1"
INDEX = "https://adc.arm.gov/elastic/file_info/_search"
DOWNLOAD = "https://adc.arm.gov/armlive/saveData"
LAT, LON = 39.0916, -28.0257


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


surface = load_module("surface", "arm-surface-sunshower-scan.py")
surface.LAT, surface.LON = LAT, LON


def credentials():
    values = {}
    for line in (ROOT / ".env.arm.local").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return f'{values["ARM_USER_ID"]}:{values["ARM_ACCESS_TOKEN"]}'


def file_index(stream, limit=5000):
    response = requests.get(INDEX, params={"q": f"file_name.keyword:{stream}.*", "size": limit},
                            headers={"User-Agent": "rainbow-connector-validation/1.0"}, timeout=120)
    response.raise_for_status()
    result = {}
    for hit in response.json().get("hits", {}).get("hits", []):
        info = hit["_source"]
        result[info["start_time"][:10].replace("-", "")] = info
    return result


def download(info, auth):
    directory = OUT / ("qcrad" if info["datastream"] == QCRAD else "met")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / info["file_name"]
    if path.exists() and path.stat().st_size == info["file_size"]:
        return path
    temporary = path.with_suffix(path.suffix + ".part")
    with requests.get(DOWNLOAD, params={"user": auth, "file": info["file_name"]},
                      headers={"User-Agent": "rainbow-connector-validation/1.0"},
                      stream=True, timeout=120) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_content(131072):
                output.write(chunk)
    temporary.replace(path)
    return path


def scan(qpath, mpath):
    matches = []
    with xr.open_dataset(qpath) as qds, xr.open_dataset(mpath) as mds:
        rain_name = next((name for name in (
            "pwd_precip_rate_mean_1min", "precip_rate_mean", "precip_rate"
        ) if name in mds), None)
        if not rain_name:
            raise RuntimeError(f"No instantaneous rain-rate field in {mpath.name}")
        qtimes = qds.time.values.astype("datetime64[s]")
        mtimes = mds.time.values.astype("datetime64[s]")
        dni = np.asarray(qds.short_direct_normal.values, dtype=float)
        temperature = np.asarray(qds.Temp_Air.values, dtype=float)
        humidity = np.asarray(qds.rh.values, dtype=float)
        rain = np.asarray(mds[rain_name].values, dtype=float)
        for mi in np.where(np.isfinite(rain) & (rain >= 0.05) & (rain < 100))[0]:
            qi = int(np.argmin(np.abs(qtimes - mtimes[mi])))
            separation = abs((qtimes[qi] - mtimes[mi]) / np.timedelta64(1, "s"))
            stamp = np.datetime_as_string(mtimes[mi], unit="s") + "Z"
            elevation = surface.solar_elevation(stamp)
            if separation > 90 or not math.isfinite(dni[qi]) or dni[qi] < 100 or not 0 < elevation < 42:
                continue
            bow_top = max(0, 42 - elevation)
            matches.append({
                "observedAt": stamp,
                "measuredDirectNormalIrradianceWm2": round(float(dni[qi]), 2),
                "measuredRainRateMmHr": round(float(rain[mi]), 3),
                "airTemperatureC": round(float(temperature[qi]), 2),
                "relativeHumidityPct": round(float(humidity[qi]), 2),
                "sunElevationDeg": round(elevation, 2),
                "score": round(min(float(dni[qi]), 600) / 12 + min(float(rain[mi]), 5) * 8 + min(bow_top, 30), 3),
            })
    return matches


def process(day, qinfo, minfo, auth):
    return [{"day": day, **item} for item in scan(download(qinfo[day], auth), download(minfo[day], auth))]


def cluster(minutes):
    minutes.sort(key=lambda item: item["observedAt"])
    groups = []
    for item in minutes:
        if not groups:
            groups.append([item])
            continue
        previous = datetime.fromisoformat(groups[-1][-1]["observedAt"].replace("Z", "+00:00"))
        current = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        if item["day"] == groups[-1][-1]["day"] and (current - previous).total_seconds() <= 3600:
            groups[-1].append(item)
        else:
            groups.append([item])
    events = []
    for group in groups:
        best = max(group, key=lambda item: item["score"])
        bow_top = max(0, 42 - best["sunElevationDeg"])
        events.append({**best, "eventStart": group[0]["observedAt"], "eventEnd": group[-1]["observedAt"],
                       "matchingMinutes": len(group), "expectedRainbowTopElevationDeg": round(bow_top, 2),
                       "reviewPriority": round(len(group) * 25 + min(best["score"], 120), 2)})
    return events


def main():
    qinfo, minfo, asi = file_index(QCRAD), file_index(MET), file_index(ASI, 2000)
    days = sorted(qinfo.keys() & minfo.keys() & asi.keys())
    auth = credentials()
    minutes, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process, day, qinfo, minfo, auth): day for day in days}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                minutes.extend(future.result())
            except Exception as error:
                errors.append({"day": futures[future], "error": str(error)})
            if number % 50 == 0 or number == len(days):
                print(f"Scanned {number}/{len(days)} ENA camera days", flush=True)
    events = [event for event in cluster(minutes) if event["airTemperatureC"] >= 5]
    events.sort(key=lambda item: item["reviewPriority"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 2, "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "site": "ARM ENA C1, Graciosa Island, Azores", "cameraDays": len(days),
               "matchingMinutes": len(minutes), "events": events, "errors": errors}
    output = OUT / "surface-sunshower-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(f"Warm-weather events: {len(events)}")
    for number, event in enumerate(events[:40], 1):
        print(number, event["observedAt"], event["matchingMinutes"], event["reviewPriority"])


if __name__ == "__main__":
    main()
