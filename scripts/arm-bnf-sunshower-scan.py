#!/usr/bin/env python3
"""Scan ARM Bankhead National Forest M1 for low-sun surface sunshowers."""
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
OUT = ROOT / "validation" / "arm-bnf"
ASI = "bnfasiskyimageM1.a1"
SIRS = "bnfsirsM1.b1"
MET = "bnfmetM1.b1"
INDEX = "https://adc.arm.gov/elastic/file_info/_search"
DOWNLOAD = "https://adc.arm.gov/armlive/saveData"
LAT, LON = 34.342481, -87.338177


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


surface = load_module("surface", "arm-surface-sunshower-scan.py")
surface.LAT, surface.LON = LAT, LON


def credentials():
    values = {}
    for line in (ROOT / ".env.arm.local").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return f'{values["ARM_USER_ID"]}:{values["ARM_ACCESS_TOKEN"]}'


def file_index(stream, limit=3000, tar_only=False):
    response = requests.get(INDEX, params={"q": f"file_name.keyword:{stream}.*", "size": limit},
                            headers={"User-Agent": "rainbow-connector-validation/1.0"}, timeout=120)
    response.raise_for_status()
    result = {}
    for hit in response.json().get("hits", {}).get("hits", []):
        info = hit["_source"]
        if tar_only and not info["file_name"].endswith(".jpg.tar"):
            continue
        day = info["start_time"][:10].replace("-", "")
        if day not in result or info["file_size"] > result[day]["file_size"]:
            result[day] = info
    return result


def download(info, auth):
    directory = OUT / ("sirs" if info["datastream"] == SIRS else "met")
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


def choose(dataset, exact, phrases):
    for name in exact:
        if name in dataset and "time" in dataset[name].dims:
            return name
    for name, item in dataset.data_vars.items():
        text = f'{name} {item.attrs.get("long_name", "")} {item.attrs.get("standard_name", "")}'.lower().replace("_", " ")
        if "qc" not in name.lower() and "time" in item.dims and all(phrase in text for phrase in phrases):
            return name
    raise RuntimeError(f"No variable matching {phrases} in {dataset.encoding.get('source')}")


def scan(spath, mpath):
    matches = []
    with xr.open_dataset(spath) as sds, xr.open_dataset(mpath) as mds:
        dni_name = choose(sds, ("short_direct_normal", "direct_normal"), ("direct", "normal"))
        rain_name = choose(mds, ("pwd_precip_rate_mean_1min", "precip_rate_mean", "precip_rate"), ("precip", "rate"))
        temp_name = choose(mds, ("temp_mean", "air_temperature"), ("temperature",))
        rh_name = choose(mds, ("rh_mean", "relh_mean", "relative_humidity"), ("relative", "humidity"))
        stimes = sds.time.values.astype("datetime64[s]")
        mtimes = mds.time.values.astype("datetime64[s]")
        dni = np.asarray(sds[dni_name].values, dtype=float)
        rain = np.asarray(mds[rain_name].values, dtype=float)
        temp = np.asarray(mds[temp_name].values, dtype=float)
        rh = np.asarray(mds[rh_name].values, dtype=float)
        qc_name = f"qc_{rain_name}"
        qc = np.asarray(mds[qc_name].values, dtype=int) if qc_name in mds else np.zeros(rain.shape, dtype=int)
        for mi in np.where(np.isfinite(rain) & (rain >= 0.05) & (rain < 100) & (qc == 0))[0]:
            si = int(np.argmin(np.abs(stimes - mtimes[mi])))
            if abs((stimes[si] - mtimes[mi]) / np.timedelta64(1, "s")) > 90:
                continue
            stamp = np.datetime_as_string(mtimes[mi], unit="s") + "Z"
            elevation = surface.solar_elevation(stamp)
            if not math.isfinite(dni[si]) or dni[si] < 80 or not 0 < elevation < 25 or temp[mi] < 5:
                continue
            matches.append({
                "observedAt": stamp,
                "measuredDirectNormalIrradianceWm2": round(float(dni[si]), 2),
                "measuredRainRateMmHr": round(float(rain[mi]), 3),
                "airTemperatureC": round(float(temp[mi]), 2),
                "relativeHumidityPct": round(float(rh[mi]), 2),
                "sunElevationDeg": round(elevation, 2),
                "expectedRainbowTopElevationDeg": round(42 - elevation, 2),
            })
    return matches


def process(day, sinfo, minfo, auth):
    return [{"day": day, **item} for item in scan(download(sinfo[day], auth), download(minfo[day], auth))]


def cluster(minutes):
    minutes.sort(key=lambda item: item["observedAt"])
    groups = []
    for item in minutes:
        if not groups:
            groups.append([item]); continue
        prior = datetime.fromisoformat(groups[-1][-1]["observedAt"].replace("Z", "+00:00"))
        current = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        if item["day"] == groups[-1][-1]["day"] and (current - prior).total_seconds() <= 3600:
            groups[-1].append(item)
        else:
            groups.append([item])
    events = []
    for group in groups:
        best = max(group, key=lambda item: item["measuredDirectNormalIrradianceWm2"])
        best = {**best, "eventStart": group[0]["observedAt"], "eventEnd": group[-1]["observedAt"],
                "matchingMinutes": len(group)}
        best["targetingScore"] = round(
            80 - abs(best["sunElevationDeg"] - 12) * 2
            + min(best["measuredDirectNormalIrradianceWm2"], 650) / 20
            + min(best["measuredRainRateMmHr"], 7) * 3
            + min(len(group), 5) * 3,
            2,
        )
        events.append(best)
    return events


def main():
    sinfo, minfo, asi = file_index(SIRS), file_index(MET), file_index(ASI, tar_only=True)
    days = sorted(sinfo.keys() & minfo.keys() & asi.keys())
    auth = credentials()
    minutes, errors = [], []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process, day, sinfo, minfo, auth): day for day in days}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                minutes.extend(future.result())
            except Exception as error:
                errors.append({"day": futures[future], "error": str(error)})
            if number % 50 == 0 or number == len(days):
                print(f"Scanned {number}/{len(days)} BNF camera days", flush=True)
    events = cluster(minutes)
    events.sort(key=lambda event: event["targetingScore"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "site": "ARM BNF M1, Bankhead National Forest, Alabama", "cameraDays": len(days),
               "matchingMinutes": len(minutes), "events": events, "errors": errors}
    output = OUT / "surface-sunshower-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(f"Events: {len(events)}")
    for number, event in enumerate(events[:30], 1):
        print(number, event["observedAt"], event["matchingMinutes"], event["targetingScore"])


if __name__ == "__main__":
    main()
