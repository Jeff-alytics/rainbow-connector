#!/usr/bin/env python3
"""Resumably download the complete compact ARM ENA all-sky movie inventory."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "arm-ena"
MOVIES = OUT / "movies"
MANIFEST = OUT / "movie-inventory.json"
STREAM = "enaasimovieC1.a1"
INDEX = "https://adc.arm.gov/elastic/file_info/_search"
DOWNLOAD = "https://adc.arm.gov/armlive/saveData"
UA = {"User-Agent": "rainbow-connector-validation/1.0"}


def credentials() -> str:
    values = {}
    for line in (ROOT / ".env.arm.local").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return f'{values["ARM_USER_ID"]}:{values["ARM_ACCESS_TOKEN"]}'


def inventory() -> list[dict]:
    response = requests.get(INDEX, params={"q": f"file_name.keyword:{STREAM}.*", "size": 5000}, headers=UA, timeout=90)
    response.raise_for_status()
    sources = [hit["_source"] for hit in response.json().get("hits", {}).get("hits", [])]
    sources.sort(key=lambda item: item["file_name"])
    return sources


def download(info: dict, auth: str) -> Path:
    MOVIES.mkdir(parents=True, exist_ok=True)
    destination = MOVIES / info["file_name"]
    if destination.exists() and destination.stat().st_size == info["file_size"]:
        return destination
    temporary = destination.with_suffix(destination.suffix + ".part")
    with requests.get(DOWNLOAD, params={"user": auth, "file": info["file_name"]}, headers=UA,
                      stream=True, timeout=240) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
    if temporary.stat().st_size != info["file_size"]:
        raise IOError(f'Incomplete {info["file_name"]}: {temporary.stat().st_size} != {info["file_size"]}')
    temporary.replace(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    files = inventory()
    if args.limit:
        files = files[:args.limit]
    auth = credentials()
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(download, info, auth): info for info in files}
        for number, future in enumerate(as_completed(futures), 1):
            info = futures[future]
            try:
                future.result()
            except Exception as error:
                errors.append({"file": info["file_name"], "error": str(error)})
            if number % 20 == 0 or number == len(files):
                print(f"Processed {number}/{len(files)} movies; {len(errors)} errors", flush=True)
    downloaded = [info for info in files if (MOVIES / info["file_name"]).exists()
                  and (MOVIES / info["file_name"]).stat().st_size == info["file_size"]]
    MANIFEST.write_text(json.dumps({"generatedAt": datetime.now(timezone.utc).isoformat(),
        "stream": STREAM, "inventoryCount": len(files), "downloadedCount": len(downloaded),
        "totalBytes": sum(info["file_size"] for info in downloaded), "errors": errors,
        "files": downloaded}, indent=2) + "\n", encoding="utf-8")
    print(MANIFEST)


if __name__ == "__main__":
    main()
