#!/usr/bin/env python3
"""Build ZIP location points from HUD residential ratios and Census tract centers.

This is an offline ingestion step.  It deliberately writes a new artifact rather
than replacing zip-centroids.json; the runtime policy will consume the artifact in
a later change.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable


CENSUS_SOURCE = (
    "https://www2.census.gov/geo/docs/reference/cenpop2020/tract/"
    "CenPop2020_Mean_TR.txt"
)


def load_zip_centroids(path: Path) -> dict[str, list]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("ZIP centroid file must contain an object")
    return value


def load_tract_centers(paths: Iterable[Path]) -> dict[str, tuple[float, float]]:
    centers: dict[str, tuple[float, float]] = {}
    for path in paths:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"STATEFP", "COUNTYFP", "TRACTCE", "LATITUDE", "LONGITUDE"}
            if not required.issubset(reader.fieldnames or ()):
                raise ValueError(f"{path} is missing Census tract fields")
            for row in reader:
                geoid = (
                    f"{row['STATEFP'].zfill(2)}"
                    f"{row['COUNTYFP'].zfill(3)}"
                    f"{row['TRACTCE'].zfill(6)}"
                )
                centers[geoid] = (float(row["LATITUDE"]), float(row["LONGITUDE"]))
    return centers


def load_hud_rows(path: Path) -> tuple[dict, list[dict]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    data = payload.get("data", payload)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("HUD file must contain data.results")
    return data, data["results"]


def coordinate_key(latitude: float, longitude: float) -> str:
    return f"{latitude:.6f},{longitude:.6f}"


def build_locations(
    zip_centroids: dict[str, list],
    hud_rows: Iterable[dict],
    tract_centers: dict[str, tuple[float, float]],
) -> tuple[dict[str, dict], dict[str, int]]:
    coordinates: dict[str, list[str]] = defaultdict(list)
    for zip_code, value in zip_centroids.items():
        coordinates[coordinate_key(float(value[2]), float(value[3]))].append(zip_code)
    shared_zips = {
        zip_code
        for cluster in coordinates.values()
        if len(cluster) > 1
        for zip_code in cluster
    }

    weighted: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for row in hud_rows:
        zip_code = str(row.get("zip", "")).zfill(5)
        if zip_code not in zip_centroids:
            continue
        weight = float(row.get("res_ratio") or 0)
        center = tract_centers.get(str(row.get("geoid", "")))
        if weight > 0 and center is not None:
            weighted[zip_code].append((weight, center[0], center[1]))

    locations: dict[str, dict] = {}
    counts = defaultdict(int)
    for zip_code, value in zip_centroids.items():
        geographic_latitude = float(value[2])
        geographic_longitude = float(value[3])
        rows = weighted.get(zip_code, [])
        total_weight = sum(row[0] for row in rows)
        if zip_code in shared_zips:
            method = "zip-shared-coordinate"
            radius = None
            requires_pin = True
        elif total_weight <= 0:
            method = "zip-po-box-only"
            radius = None
            requires_pin = True
        else:
            method = "zip-population-weighted-v1"
            radius = 4.2
            requires_pin = False

        if total_weight > 0:
            latitude = sum(weight * lat for weight, lat, _ in rows) / total_weight
            longitude = sum(weight * lon for weight, _, lon in rows) / total_weight
        else:
            latitude, longitude = geographic_latitude, geographic_longitude

        location = {
            "latitude": round(latitude, 6),
            "longitude": round(longitude, 6),
            "locationMethod": method,
            "requiresPin": requires_pin,
        }
        if radius is not None:
            location["locationUncertaintyKm"] = radius
        locations[zip_code] = location
        counts[method] += 1

    return locations, dict(sorted(counts.items()))


def build_artifact(zip_path: Path, hud_path: Path, census_paths: list[Path]) -> dict:
    zip_centroids = load_zip_centroids(zip_path)
    hud_data, hud_rows = load_hud_rows(hud_path)
    tract_centers = load_tract_centers(census_paths)
    locations, counts = build_locations(zip_centroids, hud_rows, tract_centers)
    return {
        "version": "us-zip-points-2026-v1",
        "sources": {
            "hud": {
                "year": hud_data.get("year"),
                "quarter": hud_data.get("quarter"),
                "crosswalkType": hud_data.get("crosswalk_type"),
            },
            "census": {"tractCentersUrl": CENSUS_SOURCE, "vintage": "2020"},
        },
        "counts": counts,
        "locations": locations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip-centroids", type=Path, default=Path("zip-centroids.json"))
    parser.add_argument("--hud-json", type=Path, required=True)
    parser.add_argument("--census-tract", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    artifact = build_artifact(args.zip_centroids, args.hud_json, args.census_tract)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(artifact, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "counts": artifact["counts"]}))


if __name__ == "__main__":
    main()
