#!/usr/bin/env python
"""Decode retained MRMS keys once into compact, resumable storm-object scans."""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from mrms_source import download_and_expand, object_from_key, open_precip_grid
from opportunity_ledger import STORM_OBJECT_METHOD_VERSION, storm_objects
from rain_footprint import build_storm_segmentation_sidecar

SCHEMA_VERSION = "storm-object-scan-artifact.v1"


def canonical_json(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + chr(10)).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def artifact_name(observed_at: str) -> str:
    return "scan-" + observed_at.replace("-", "").replace(":", "") + ".json.gz"


def object_row_index(objects: list[dict]) -> dict[int, tuple[list[int], list[tuple[int, str]]]]:
    rows: dict[int, list[tuple[int, int, str]]] = {}
    for item in objects:
        for row, first, last, *_ in item.get("runs") or []:
            rows.setdefault(int(row), []).append((int(first), int(last), item["componentId"]))
    result = {}
    for row, intervals in rows.items():
        intervals.sort()
        result[row] = (
            [item[0] for item in intervals],
            [(item[1], item[2]) for item in intervals],
        )
    return result


def attach_seeds(sidecar: dict, objects: list[dict], seeds: list[dict]) -> tuple[list[dict], dict[str, int], int]:
    grid = sidecar["grid"]
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    index = object_row_index(objects)
    attached = []
    counts = {item["componentId"]: 0 for item in objects}
    unattached = 0
    for seed in seeds:
        rain_lat, rain_lon = seed.get("rainLat"), seed.get("rainLon")
        if rain_lat is None or rain_lon is None:
            unattached += 1
            continue
        row = round((float(rain_lat) - lat0) / dlat)
        column = round((float(rain_lon) - lon0) / dlon)
        starts, intervals = index.get(row, ([], []))
        position = bisect.bisect_right(starts, column) - 1
        if position < 0 or column > intervals[position][0]:
            unattached += 1
            continue
        component_id = intervals[position][1]
        counts[component_id] += 1
        attached.append({"stormComponentId": component_id, "seed": seed})
    return attached, counts, unattached


def build_artifact(row: dict, field, expanded_path: Path, source_record_sha256: str) -> dict:
    latitudes = field.coords["latitude"].values
    longitudes = field.coords["longitude"].values.copy()
    longitudes[longitudes > 180] -= 360
    observed_at = parse_utc(row["observedAt"])
    sidecar = build_storm_segmentation_sidecar(
        latitudes, longitudes, field.values, observed_at, row["s3Key"]
    )
    objects = storm_objects(sidecar)
    attached, attachment_counts, unattached = attach_seeds(
        sidecar, objects, row.get("allSeeds") or []
    )
    coverage = {"0": 0, "1": 0, "2+": 0}
    for count in attachment_counts.values():
        coverage["0" if count == 0 else "1" if count == 1 else "2+"] += 1
    return {
        "schemaVersion": SCHEMA_VERSION,
        "methodVersion": STORM_OBJECT_METHOD_VERSION,
        "observedAt": row["observedAt"],
        "s3Key": row["s3Key"],
        "sourceRecordSha256": source_record_sha256,
        "expandedMrmsSha256": hashlib.sha256(expanded_path.read_bytes()).hexdigest(),
        "grid": sidecar["grid"],
        "stormSegmentation": sidecar["stormSegmentation"],
        "stormObjects": objects,
        "attachedSeeds": attached,
        "diagnostics": {
            "inputSeeds": len(row.get("allSeeds") or []),
            "attachedSeeds": len(attached),
            "unattachedSeeds": unattached,
            "seedAttachmentCoveragePerObject": coverage,
        },
    }


def write_gzip_exclusive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with temporary.open("xb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as stream:
            stream.write(raw)
    try:
        temporary.replace(path)
    except OSError:
        if path.exists():
            temporary.unlink(missing_ok=True)
        else:
            raise


def session_with_retries() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "HEAD")),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    pool_files = sorted(args.pool_dir.glob("pool-*.jsonl"))
    if not pool_files:
        parser.error(f"No pool JSONL files found in {args.pool_dir}")
    session = session_with_retries()
    completed = skipped = 0
    for pool_path in pool_files:
        with pool_path.open("rb") as source:
            for raw_line in source:
                if not raw_line.strip():
                    continue
                row = json.loads(raw_line)
                if row.get("status") != "ok" or "allSeeds" not in row:
                    continue
                output_path = args.output_dir / row["observedAt"][:10] / artifact_name(row["observedAt"])
                if output_path.exists():
                    skipped += 1
                    continue
                obj = object_from_key(row["s3Key"])
                expanded = download_and_expand(obj, args.cache_dir, session=session)
                try:
                    field = open_precip_grid(expanded)
                    artifact = build_artifact(row, field, expanded, sha256_bytes(raw_line))
                    write_gzip_exclusive(output_path, canonical_json(artifact))
                finally:
                    for stale in args.cache_dir.glob(expanded.name + "*"):
                        stale.unlink(missing_ok=True)
                completed += 1
                print(json.dumps({
                    "completed": completed,
                    "skipped": skipped,
                    "observedAt": row["observedAt"],
                    "objects": len(artifact["stormObjects"]),
                    "attachedSeeds": artifact["diagnostics"]["attachedSeeds"],
                }), flush=True)
                if args.limit and completed >= args.limit:
                    return 0
    print(json.dumps({"done": True, "completed": completed, "skipped": skipped}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
