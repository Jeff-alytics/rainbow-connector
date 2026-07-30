#!/usr/bin/env python3
"""Bulk-indexed scan for simultaneous measured rain and direct sunlight."""

from __future__ import annotations

import importlib.util
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"
SPEC = importlib.util.spec_from_file_location("surface_scan", ROOT / "scripts" / "arm-surface-sunshower-scan.py")
surface = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(surface)


def file_index(stream, session):
    response = session.get(
        surface.INDEX,
        params={"q": f"file_name.keyword:{stream}.*", "size": 5000},
        timeout=60,
    )
    response.raise_for_status()
    indexed = {}
    for hit in response.json().get("hits", {}).get("hits", []):
        info = hit["_source"]
        match = re.search(r"\.(20\d{6})\.", info["file_name"])
        if match:
            indexed[match.group(1)] = info
    return indexed


def download(info, stream, auth):
    directory = VALIDATION / ("qcrad" if stream == surface.QCRAD else "vdis")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / info["file_name"]
    if path.exists() and path.stat().st_size == info["file_size"]:
        return path
    session = requests.Session()
    session.headers["User-Agent"] = "rainbow-connector-validation/1.0"
    with session.get(surface.DOWNLOAD, params={"user": auth, "file": info["file_name"]}, stream=True, timeout=120) as response:
        response.raise_for_status()
        temporary = path.with_suffix(path.suffix + ".part")
        with temporary.open("wb") as output:
            for chunk in response.iter_content(131072):
                output.write(chunk)
        temporary.replace(path)
    return path


def process(day, qinfo, vinfo, auth):
    qpath = download(qinfo[day], surface.QCRAD, auth)
    vpath = download(vinfo[day], surface.VDIS, auth)
    return [{"day": day, **item} for item in surface.scan_day(qpath, vpath)]


def main():
    manifest = json.loads((VALIDATION / "manifest.json").read_text(encoding="utf-8"))
    wanted = {pair["event"]["observedAt"][:10].replace("-", "") for pair in manifest["pairs"]}
    session = requests.Session()
    session.headers["User-Agent"] = "rainbow-connector-validation/1.0"
    qinfo = file_index(surface.QCRAD, session)
    vinfo = file_index(surface.VDIS, session)
    days = sorted(wanted & qinfo.keys() & vinfo.keys())
    auth = surface.credentials()
    candidates = []
    errors = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(process, day, qinfo, vinfo, auth): day for day in days}
        for number, future in enumerate(as_completed(futures), 1):
            try:
                candidates.extend(future.result())
            except Exception as error:
                errors.append({"day": futures[future], "error": str(error)})
            if number % 20 == 0 or number == len(days):
                print(f"Scanned {number}/{len(days)} overlapping days", flush=True)
    candidates.sort(key=lambda item: item["score"], reverse=True)
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "purpose": "Measured surface sunshower discovery using simultaneous QCRAD direct sun and VDIS rain rate.",
        "qcradStream": surface.QCRAD,
        "precipitationStream": surface.VDIS,
        "daysRequested": len(wanted),
        "daysWithBothStreams": len(days),
        "errors": errors,
        "candidates": candidates,
    }
    output = VALIDATION / "surface-sunshower-ranked.json"
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(output)
    for number, item in enumerate(candidates[:30], 1):
        print(number, item["observedAt"], item["score"], item["measuredDirectNormalIrradianceWm2"], item["measuredRainRateMmHr"])


if __name__ == "__main__":
    main()
