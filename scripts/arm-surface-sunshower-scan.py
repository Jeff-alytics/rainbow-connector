#!/usr/bin/env python3
"""Find measured surface sunshowers at ARM SGP C1."""

from __future__ import annotations

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
QCRAD = "sgpqcradbrs1longC1.c1"
VDIS = "sgpvdisquantsC1.c1"
INDEX = "https://adc.arm.gov/elastic/file_info/_search"
DOWNLOAD = "https://adc.arm.gov/armlive/saveData"
LAT, LON = 36.607322, -97.487643


def credentials():
    values = {}
    for line in (ROOT / ".env.arm.local").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return f'{values["ARM_USER_ID"]}:{values["ARM_ACCESS_TOKEN"]}'


def archive_file(stream, day, auth, session):
    directory = VALIDATION / ("qcrad" if stream == QCRAD else "vdis")
    directory.mkdir(parents=True, exist_ok=True)
    result = session.get(INDEX, params={"q": f"file_name.keyword:{stream}.{day}*", "size": 2}, timeout=30)
    result.raise_for_status()
    hits = result.json().get("hits", {}).get("hits", [])
    if not hits:
        return None
    info = hits[0]["_source"]
    path = directory / info["file_name"]
    if not path.exists() or path.stat().st_size != info["file_size"]:
        with session.get(DOWNLOAD, params={"user": auth, "file": info["file_name"]}, stream=True, timeout=90) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for chunk in response.iter_content(131072):
                    output.write(chunk)
    return path


def variable(dataset, phrase, preferred):
    if preferred in dataset and "time" in dataset[preferred].dims:
        return preferred
    for name, item in dataset.data_vars.items():
        text = f'{name} {item.attrs.get("long_name", "")} {item.attrs.get("standard_name", "")}'.lower().replace("_", " ")
        if phrase in text and "qc" not in name.lower() and "time" in item.dims:
            return name
    raise RuntimeError(f"No {phrase} variable in {dataset.encoding.get('source')}")


def solar_elevation(timestamp):
    date = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    rad = math.pi / 180
    days = date.timestamp() / 86400 - 0.5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + 0.98560028 * days)
    longitude = anomaly + rad * (1.9148 * math.sin(anomaly) + 0.02 * math.sin(2 * anomaly) + 0.0003 * math.sin(3 * anomaly)) + rad * 102.9372 + math.pi
    declination = math.asin(math.sin(rad * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(math.sin(longitude) * math.cos(rad * 23.4397), math.cos(longitude))
    hour_angle = rad * (280.16 + 360.9856235 * days) + LON * rad - right_ascension
    latitude = LAT * rad
    return math.asin(math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)) / rad


def scan_day(qpath, vpath):
    with xr.open_dataset(qpath) as qds, xr.open_dataset(vpath) as vds:
        qname = variable(qds, "direct normal", "short_direct_normal")
        rname = variable(vds, "rain rate", "rain_rate")
        qtimes = qds.time.values.astype("datetime64[ns]")
        qvalues = np.asarray(qds[qname].values, dtype=float)
        rtimes = vds.time.values.astype("datetime64[ns]")
        rvalues = np.asarray(vds[rname].values, dtype=float)
        matches = []
        for rtime, rain in zip(rtimes, rvalues):
            if not math.isfinite(rain) or rain < 0.1:
                continue
            index = int(np.argmin(np.abs(qtimes - rtime)))
            separation = abs((qtimes[index] - rtime) / np.timedelta64(1, "s"))
            dni = float(qvalues[index])
            stamp = np.datetime_as_string(rtime, unit="s") + "Z"
            elevation = solar_elevation(stamp)
            if separation > 90 or not math.isfinite(dni) or dni < 100 or not 0 < elevation < 42:
                continue
            rain_score = max(0, 1 - abs(math.log10(max(rain, 0.1)) - math.log10(1.5)) / 1.5) * 30
            score = min(dni, 900) / 15 + rain_score
            matches.append({
                "observedAt": stamp,
                "measuredDirectNormalIrradianceWm2": round(dni, 2),
                "measuredRainRateMmHr": round(float(rain), 3),
                "sunElevationDeg": round(elevation, 2),
                "score": round(score, 3),
            })
    if not matches:
        return []
    matches.sort(key=lambda item: item["observedAt"])
    groups = [[matches[0]]]
    for item in matches[1:]:
        prior = datetime.fromisoformat(groups[-1][-1]["observedAt"].replace("Z", "+00:00"))
        current = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        (groups[-1] if (current - prior).total_seconds() <= 180 else groups.append([item]))
        if groups[-1][-1] is not item:
            groups[-1].append(item)
    return [max(group, key=lambda item: item["score"]) for group in groups]
def process_day(day, auth):
    session = requests.Session()
    session.headers["User-Agent"] = "rainbow-connector-validation/1.0"
    qpath = archive_file(QCRAD, day, auth, session)
    vpath = archive_file(VDIS, day, auth, session)
    if not qpath or not vpath:
        return []
    return [{"day": day, **item} for item in scan_day(qpath, vpath)]




def main():
    manifest = json.loads((VALIDATION / "manifest.json").read_text(encoding="utf-8"))
    days = sorted({pair["event"]["observedAt"][:10].replace("-", "") for pair in manifest["pairs"]})
    auth = credentials()
    candidates = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process_day, day, auth): day for day in days}
        for index, future in enumerate(as_completed(futures), 1):
            day = futures[future]
            try:
                candidates.extend(future.result())
            except Exception as error:
                print(f"Skipped {day}: {error}", flush=True)
            if index % 20 == 0 or index == len(days):
                print(f"Scanned {index}/{len(days)} days", flush=True)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Measured surface sunshower discovery; candidates require simultaneous QCRAD direct sun and VDIS rain rate.",
        "qcradStream": QCRAD,
        "precipitationStream": VDIS,
        "candidates": candidates,
    }
    output = VALIDATION / "surface-sunshower-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    for number, item in enumerate(candidates[:30], 1):
        print(number, item["observedAt"], item["score"], item["measuredDirectNormalIrradianceWm2"], item["measuredRainRateMmHr"])


if __name__ == "__main__":
    main()
