#!/usr/bin/env python
"""Build a post-interim, prediction-blind bow-to-storm compatibility artifact."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "worker"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from detector_core import solar_position
from opportunity_ledger import observer_swath, swath_contains
from scripts.storm_target_geometry import (
    content_sha256,
    distance_km,
    distance_to_object_km,
    file_sha256,
    load_scan_artifact,
    nearest_scan,
    parse_utc,
)

SCHEMA_VERSION = "time-aligned-storm-targets.v2"
SITE_ID = re.compile(r"^site(\d+)-")


def relative_bearing_degrees(observer_lat, observer_lon, rain_lat, rain_lon, center_bearing):
    north = (rain_lat - observer_lat) * 111
    east = (rain_lon - observer_lon) * 111 * math.cos(math.radians(observer_lat))
    bearing = (math.degrees(math.atan2(east, north)) + 360) % 360
    return (bearing - center_bearing + 180) % 360 - 180


def occupancy_metrics(item, artifact, core, observer_lat, observer_lon, observed_at):
    grid = artifact["grid"]
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    elevation, sun_bearing = solar_position(observed_at, observer_lat, observer_lon)
    anti_solar = (sun_bearing + 180) % 360
    half_extent = math.degrees(math.acos(max(-1, min(
        1, math.cos(math.radians(42)) / math.cos(math.radians(elevation))
    ))))
    envelope_bins, core_bins = set(), set()
    for row, first, last, *_ in item["runs"]:
        rain_lat = lat0 + int(row) * dlat
        for column in range(int(first), int(last) + 1):
            rain_lon = lon0 + column * dlon
            distance = distance_km(observer_lat, observer_lon, rain_lat, rain_lon)
            if not 5 <= distance <= 40:
                continue
            relative = relative_bearing_degrees(
                observer_lat, observer_lon, rain_lat, rain_lon, anti_solar
            )
            if -half_extent <= relative <= half_extent:
                degree_bin = math.floor(relative)
                envelope_bins.add(degree_bin)
                if (int(row), column) in core:
                    core_bins.add(degree_bin)
    return {
        "bowArcEnvelopeOccupiedDeg": len(envelope_bins),
        "bowArcCoreOccupiedDeg": len(core_bins),
        "bowArcHalfExtentDeg": round(half_extent, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-manifest",
        type=Path,
        default=ROOT / "validation/storm-object-replay-gate/scan-artifact-manifest.json",
    )
    parser.add_argument(
        "--targets",
        type=Path,
        default=ROOT / "validation/causal-replay-gate/bow-evaluation-targets.json",
    )
    parser.add_argument(
        "--sites",
        type=Path,
        default=ROOT / "validation/faa/sites.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--retain-scan-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.artifact_manifest.read_text(encoding="utf-8"))
    sources = manifest["files"]
    config = json.loads(args.config.read_text(encoding="utf-8"))
    old_targets = json.loads(args.targets.read_text(encoding="utf-8"))["targets"]
    sites = {
        str(item["siteId"]): item
        for item in json.loads(args.sites.read_text(encoding="utf-8-sig"))
    }
    targets = []
    for old in old_targets:
        match = SITE_ID.match(old["eventId"])
        if not match or match.group(1) not in sites:
            raise ValueError(f"FAA site missing for {old['eventId']}")
        site = sites[match.group(1)]
        observer_lat = float(site["latitude"])
        observer_lon = float(site["longitude"])
        time_arms = []
        for offset_minutes in config["scanOffsetsMinutes"]:
            shifted = parse_utc(old["targetAt"]).timestamp() + offset_minutes * 60
            shifted_at = parse_utc(old["targetAt"]).fromtimestamp(shifted, tz=parse_utc(old["targetAt"]).tzinfo)
            shifted_iso = shifted_at.isoformat().replace("+00:00", "Z")
            source = nearest_scan(
                shifted_iso, sources, config["nearestScanMaximumGapMinutes"]
            )
            if source is None:
                time_arms.append({
                    "offsetMinutes": offset_minutes,
                    "assignment": "no_scan_within_5min",
                    "compatibleStormComponents": [],
                })
                continue
            source_path = Path(source["path"])
            if file_sha256(source_path) != source["compressedSha256"]:
                raise ValueError(f"Scan artifact hash mismatch: {source_path}")
            args.retain_scan_dir.mkdir(parents=True, exist_ok=True)
            retained_path = args.retain_scan_dir / source_path.name
            if not retained_path.exists():
                with source_path.open("rb") as incoming, retained_path.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
            artifact = load_scan_artifact(source_path)
            core_cells = {
                (int(row), column)
                for row, first, last, *_ in artifact["stormSegmentation"]["coreRuns"]
                for column in range(int(first), int(last) + 1)
            }
            sidecar = {"grid": artifact["grid"]}
            observed_at = parse_utc(artifact["observedAt"])
            compatible = []
            prefiltered = 0
            closest_swath_near_miss = None
            for item in artifact["stormObjects"]:
                envelope_distance = distance_to_object_km(
                    observer_lat, observer_lon, item, artifact["grid"]
                )
                if not 0 <= envelope_distance <= 45:
                    continue
                prefiltered += 1
                swath = observer_swath(item, sidecar, observed_at)
                inside, swath_distance = swath_contains(
                    swath, observer_lat, observer_lon
                )
                if not inside:
                    if swath_distance is not None:
                        closest_swath_near_miss = min(
                            closest_swath_near_miss
                            if closest_swath_near_miss is not None else math.inf,
                            swath_distance,
                        )
                    continue
                compatible.append({
                    "componentId": item["componentId"],
                    "envelopeDistanceKm": round(envelope_distance, 3),
                    "observerSwathDistanceKm": round(swath_distance or 0.0, 3),
                    "exactSwathContainment": swath_distance == 0,
                    "envelopeCellCount": item["cellCount"],
                    "coreCellCount": item["coreCellCount"],
                    **occupancy_metrics(
                        item, artifact, core_cells,
                        observer_lat, observer_lon, observed_at
                    ),
                })
            compatible.sort(key=lambda item: (
                -item["bowArcEnvelopeOccupiedDeg"],
                not item["exactSwathContainment"],
                -item["bowArcCoreOccupiedDeg"],
                item["componentId"],
            ))
            time_arms.append({
                "offsetMinutes": offset_minutes,
                "scanTime": artifact["observedAt"],
                "scanGapMinutes": round(abs(
                    (parse_utc(artifact["observedAt"]) - shifted_at).total_seconds()
                ) / 60, 3),
                "sourceScanPath": str(retained_path),
                "sourceScanSha256": file_sha256(retained_path),
                "totalStormComponents": len(artifact["stormObjects"]),
                "prefilteredStormCount": prefiltered,
                "closestSwathNearMissKm": (
                    round(closest_swath_near_miss, 3)
                    if closest_swath_near_miss is not None else None
                ),
                "assignment": (
                    "unique_physical_storm"
                    if len(compatible) == 1
                    else "ambiguous_physical_storms"
                    if compatible
                    else "no_physical_storm"
                ),
                "middleSelectedComponentId": (
                    compatible[0]["componentId"] if compatible else None
                ),
                "compatibleStormComponents": compatible,
            })
        primary = next(item for item in time_arms if item["offsetMinutes"] == 0)
        targets.append({
            "eventId": old["eventId"],
            "targetAt": old["targetAt"],
            "leadProxy": old["leadProxy"],
            "siteId": match.group(1),
            "siteName": site.get("siteName"),
            "observer": {"lat": observer_lat, "lon": observer_lon},
            "assignment": primary["assignment"],
            "timeArms": time_arms,
        })
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "status": "post_interim_diagnostic_not_frozen_gate",
        "createdAfterInterimResultWasRead": True,
        "usesPredictionScores": False,
        "usesEligibility": False,
        "usesSchedulerTokens": False,
        "method": {
            "observerCoordinate": "actual_faa_site_coordinate",
            "radarScan": "nearest_decoded_MRMS_scan_within_5_minutes_of_targetAt",
            "rainPrefilterKm": [0, 45],
            "physicalCompatibility": "production_0.025deg_observer_swath_contains_site",
            "ambiguityRule": "retain_all_compatible_components; never select_by_alert_outcome",
            "analysisArms": config["analysisArms"],
        },
        "sources": [
            {"path": str(path), "sha256": file_sha256(path)}
            for path in (args.artifact_manifest, args.targets, args.sites)
        ],
        "config": {"path": str(args.config), "sha256": file_sha256(args.config)},
        "targetCount": len(targets),
        "assignmentCounts": {
            status: sum(item["assignment"] == status for item in targets)
            for status in (
                "unique_physical_storm",
                "ambiguous_physical_storms",
                "no_physical_storm",
                "no_scan_within_5min",
            )
        },
        "targets": targets,
    }
    payload["contentSha256"] = content_sha256(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline=chr(10)) as stream:
        stream.write(json.dumps(payload, indent=2) + chr(10))
    print(json.dumps({
        "output": str(args.output),
        "assignmentCounts": payload["assignmentCounts"],
        "contentSha256": payload["contentSha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
