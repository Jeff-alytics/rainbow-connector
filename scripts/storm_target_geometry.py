"""Prediction-neutral geometry and hashing helpers for target artifacts."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_sha256(payload) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_scan_artifact(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def nearest_scan(target_at: str, sources: list[dict], maximum_minutes: float = 5) -> dict | None:
    target = parse_utc(target_at)
    best = min(sources, key=lambda item: abs((parse_utc(item["observedAt"]) - target).total_seconds()))
    gap = abs((parse_utc(best["observedAt"]) - target).total_seconds()) / 60
    return best if gap <= maximum_minutes else None


def distance_km(lat1, lon1, lat2, lon2) -> float:
    north = (float(lat1) - float(lat2)) * 111
    east = (float(lon1) - float(lon2)) * 111 * math.cos(math.radians((float(lat1) + float(lat2)) / 2))
    return math.hypot(north, east)


def distance_to_object_km(target_lat: float, target_lon: float, item: dict, grid: dict) -> float:
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    target_row = (float(target_lat) - lat0) / dlat
    target_column = (float(target_lon) - lon0) / dlon
    best = math.inf
    for row, first, last, *_ in item.get("runs") or []:
        row = int(row)
        column = min(max(target_column, int(first)), int(last))
        lat = lat0 + row * dlat
        lon = lon0 + column * dlon
        best = min(best, distance_km(target_lat, target_lon, lat, lon))
    return best
