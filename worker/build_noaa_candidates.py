#!/usr/bin/env python
"""Build the radar-first shortlist consumed by the sunlight enrichment stage."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from detector_core import observer_seeds_from_rain_grid
from mrms_source import MAX_AGE_MINUTES, discover_latest, download_and_expand, open_precip_grid, source_metadata
from rain_footprint import rain_footprint_id


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a NOAA MRMS rain-edge shortlist")
    parser.add_argument("--cache-dir", default=".worker-cache/mrms")
    parser.add_argument("--output")
    parser.add_argument("--maximum", type=int, default=400)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--metadata-only", action="store_true")
    return parser.parse_args()


def build(args: argparse.Namespace, research_artifacts: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    obj = discover_latest(now)
    radar_health = source_metadata(obj, now)
    if not radar_health["fresh"]:
        raise RuntimeError(
            f"Latest MRMS frame is {radar_health['ageMinutes']} minutes old; maximum is {MAX_AGE_MINUTES}"
        )
    artifact = {
        "stage": "radar-shortlist",
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "sourceHealth": {"radar": radar_health},
        "shortlist": [],
    }
    if args.metadata_only:
        return artifact

    grib_path = download_and_expand(obj, Path(args.cache_dir))
    field = open_precip_grid(grib_path)
    latitude = field.coords.get("latitude")
    longitude = field.coords.get("longitude")
    if latitude is None or longitude is None or latitude.ndim != 1 or longitude.ndim != 1:
        raise RuntimeError("Expected MRMS regular latitude/longitude coordinates")
    longitude_values = longitude.values.copy()
    # MRMS GRIB uses 0..360 degrees east; the rest of the detector uses signed longitude.
    longitude_values[longitude_values > 180] -= 360
    latitude_values = latitude.values
    rate_values = field.values
    radar_diagnostics = {}
    artifact["shortlist"] = observer_seeds_from_rain_grid(
        latitude_values,
        longitude_values,
        rate_values,
        obj.observed_at,
        stride=args.stride,
        maximum=args.maximum,
        diagnostics=radar_diagnostics,
    )
    footprint_id = rain_footprint_id(obj.observed_at)
    if research_artifacts is not None:
        # Retain the already-decoded grid in memory; full sidecar encoding and
        # S3 storage happen only after public publish and subscriber notification.
        research_artifacts["rainFootprintContext"] = {
            "latitudes": latitude_values, "longitudes": longitude_values, "rates": rate_values,
            "observedAt": obj.observed_at, "sourceKey": obj.key, "candidates": artifact["shortlist"],
            "radarObservedAt": radar_health["observedAt"],
        }
    artifact["sourceHealth"]["radar"]["rainFootprintId"] = footprint_id
    artifact["diagnostics"] = {
        "radarFirst": True,
        "inputGridShape": list(field.shape),
        "shortlistedObservers": len(artifact["shortlist"]),
        **radar_diagnostics,
        "nextStage": "batch DNI plus GOES clear-mask corroboration",
    }
    return artifact


def main() -> int:
    args = arguments()
    artifact = build(args)
    payload = json.dumps(artifact, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
