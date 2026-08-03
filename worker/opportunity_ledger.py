"""Deterministic, camera-independent Opportunity Ledger research records."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import numpy as np
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from shapely import contains_xy
from shapely.geometry import shape

from detector_core import CONUS_BOUNDS, solar_position

SCHEMA_VERSION = "opportunity-ledger.v1"
STORM_OBJECT_SCHEMA_VERSION = "storm-object-ledger.v1"
STORM_OBJECT_METHOD_VERSION = "mrms-hysteresis-object-lineage-2026-08-v1"
METHOD_VERSION = "observer-swath-physical-envelope-2026-07-v3-viable-only"
SWATH_RESOLUTION_DEG = 0.025
SWATH_TOLERANCE_KM = 3.0
RAIN_DISTANCE_KM = (5.0, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0, 22.5, 25.0, 27.5, 30.0, 32.5, 35.0, 37.5, 40.0)
ANGLE_STEP_DEG = 3.0
REPRESENTATIVE_SPACING_KM = 15.0
CAMERA_CANDIDATE_MAX_KM = 40.0
RAIN_EDGE_SAMPLE_KM = 2.5


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


class _DisjointSet:
    def __init__(self, count: int):
        self.parent = list(range(count))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def rain_components(sidecar: dict) -> list[dict]:
    """Build eight-connected scan components directly from tiered row runs."""
    source_runs = sorted(
        [[int(row), int(first), int(last), int(tier)] for row, first, last, tier in sidecar.get("runs") or []],
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    if not source_runs:
        return []
    groups = _DisjointSet(len(source_runs))
    rows: dict[int, list[int]] = defaultdict(list)
    for index, run in enumerate(source_runs):
        rows[run[0]].append(index)
    for row, indices in rows.items():
        for left, right in zip(indices, indices[1:]):
            if source_runs[right][1] <= source_runs[left][2] + 1:
                groups.union(left, right)
        for current in indices:
            _, first, last, _ = source_runs[current]
            for previous in rows.get(row - 1, []):
                _, old_first, old_last, _ = source_runs[previous]
                if first <= old_last + 1 and old_first <= last + 1:
                    groups.union(current, previous)
    members: dict[int, list[list[int]]] = defaultdict(list)
    for index, run in enumerate(source_runs):
        members[groups.find(index)].append(run)
    observed = str(sidecar["observedAt"])
    components = []
    for runs in members.values():
        runs.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        identity = _hash({"observedAt": observed, "runs": runs})[:16]
        component_id = "mrms-component-" + observed.replace("-", "").replace(":", "").replace("Z", "Z-") + identity
        components.append({
            "componentId": component_id,
            "eventId": "rain-event-" + identity,
            "observedAt": observed,
            "runs": runs,
            "bounds": {
                "rowMin": min(run[0] for run in runs), "rowMax": max(run[0] for run in runs),
                "columnMin": min(run[1] for run in runs), "columnMax": max(run[2] for run in runs),
            },
            "cellCount": sum(run[2] - run[1] + 1 for run in runs),
            "maximumTier": max(run[3] for run in runs),
            "lineageScanCount": 1,
        })
    return sorted(components, key=lambda item: item["componentId"])


def _cells_from_runs(runs: list[list[int]]) -> set[tuple[int, int]]:
    return {
        (int(row), column)
        for row, first, last, *_ in runs
        for column in range(int(first), int(last) + 1)
    }


def _connected_groups(cells: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    remaining = set(cells)
    groups = []
    while remaining:
        start = remaining.pop()
        group = {start}
        stack = [start]
        while stack:
            row, column = stack.pop()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    neighbor = row + dr, column + dc
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        group.add(neighbor)
                        stack.append(neighbor)
        groups.append(group)
    return groups


def _component_runs(cells: set[tuple[int, int]]) -> list[list[int]]:
    return [[row, first, last, 1] for row, first, last in encode_swath(cells)]


def storm_objects(sidecar: dict) -> list[dict]:
    """Segment exact MRMS rates with core/envelope hysteresis.

    Every >=2 mm/hr connected core owns envelope cells reachable through
    >=0.5 mm/hr rain. Multi-core envelopes are divided by deterministic
    eight-neighbor distance, so a light-rain bridge cannot weld identities.
    """
    segmentation = sidecar.get("stormSegmentation") or {}
    envelope = _cells_from_runs(segmentation.get("envelopeRuns") or [])
    core = _cells_from_runs(segmentation.get("coreRuns") or [])
    if not envelope or not core:
        return []
    core_groups = _connected_groups(core)
    core_groups.sort(key=lambda cells: min(cells))
    owner_by_cell: dict[tuple[int, int], int] = {}
    distance_by_cell: dict[tuple[int, int], int] = {}
    queue = []
    for owner, cells in enumerate(core_groups):
        for row, column in cells:
            owner_by_cell[(row, column)] = owner
            distance_by_cell[(row, column)] = 0
            heapq.heappush(queue, (0, owner, row, column))
    while queue:
        distance, owner, row, column = heapq.heappop(queue)
        cell = row, column
        if distance_by_cell.get(cell) != distance or owner_by_cell.get(cell) != owner:
            continue
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                neighbor = row + dr, column + dc
                if neighbor not in envelope:
                    continue
                proposal = distance + 1
                current = distance_by_cell.get(neighbor)
                current_owner = owner_by_cell.get(neighbor)
                if current is not None and (current < proposal or (current == proposal and current_owner <= owner)):
                    continue
                distance_by_cell[neighbor] = proposal
                owner_by_cell[neighbor] = owner
                heapq.heappush(queue, (proposal, owner, neighbor[0], neighbor[1]))
    cells_by_owner: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for cell, owner in owner_by_cell.items():
        cells_by_owner[owner].add(cell)
    observed = str(sidecar["observedAt"])
    objects = []
    for owner, cells in sorted(cells_by_owner.items()):
        runs = _component_runs(cells)
        core_cells = core_groups[owner]
        identity = _hash({"observedAt": observed, "coreRuns": _component_runs(core_cells)})[:16]
        objects.append({
            "componentId": "storm-object-" + observed.replace("-", "").replace(":", "").replace("Z", "Z-") + identity,
            "eventId": "storm-family-" + identity,
            "ancestorEventIds": ["storm-family-" + identity],
            "observedAt": observed,
            "runs": runs,
            "bounds": {
                "rowMin": min(row for row, _ in cells), "rowMax": max(row for row, _ in cells),
                "columnMin": min(column for _, column in cells), "columnMax": max(column for _, column in cells),
            },
            "cellCount": len(cells),
            "coreCellCount": len(core_cells),
            "lineageScanCount": 1,
        })
    return objects


def _component_gap(left: dict, right: dict, maximum: int = 12) -> int:
    """Return the exact Chebyshev cell gap within the motion window."""
    left_runs, right_runs = left.get("runs") or [], right.get("runs") or []
    if len(left_runs) > len(right_runs):
        left_runs, right_runs = right_runs, left_runs
    right_by_row: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row, first, last, _ in right_runs:
        right_by_row[int(row)].append((int(first), int(last)))
    best = maximum + 1
    for row, first, last, _ in left_runs:
        row, first, last = int(row), int(first), int(last)
        for other_row in range(row - maximum, row + maximum + 1):
            row_gap = abs(row - other_row)
            if row_gap >= best:
                continue
            for other_first, other_last in right_by_row.get(other_row, ()):
                column_gap = max(0, other_first - last, first - other_last)
                best = min(best, max(row_gap, column_gap))
    return best


def _overlap_cell_count(left: dict, right: dict) -> int:
    left_rows: dict[int, list[tuple[int, int]]] = defaultdict(list)
    right_rows: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row, first, last, _ in left.get("runs") or []:
        left_rows[int(row)].append((int(first), int(last)))
    for row, first, last, _ in right.get("runs") or []:
        right_rows[int(row)].append((int(first), int(last)))
    overlap = 0
    for row in left_rows.keys() & right_rows.keys():
        for left_first, left_last in left_rows[row]:
            for right_first, right_last in right_rows[row]:
                overlap += max(0, min(left_last, right_last) - max(left_first, right_first) + 1)
    return overlap


def link_lineage(
    previous: dict | None,
    current: dict,
    maximum_motion_cells: int = 12,
    event_key: str = "rainEvents",
    edge_key: str = "lineageEdges",
) -> dict:
    """Attach stable event IDs and explicit continuation/split/merge edges."""
    if not previous:
        return current
    old = previous.get(event_key) or []
    new = current.get(event_key) or []
    old_runs_by_row: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    old_motion_by_row: dict[int, set[int]] = defaultdict(set)
    for index, candidate in enumerate(old):
        for row, first, last, _ in candidate.get("runs") or []:
            old_runs_by_row[int(row)].append((int(first), int(last), index))
        bounds = candidate["bounds"]
        for row in range(bounds["rowMin"] - maximum_motion_cells, bounds["rowMax"] + maximum_motion_cells + 1):
            old_motion_by_row[row].add(index)
    parents: dict[str, list[dict]] = {}
    link_details: dict[tuple[str, str], dict] = {}
    children: dict[str, list[dict]] = defaultdict(list)
    for item in new:
        overlap_by_index: dict[int, int] = defaultdict(int)
        for row, first, last, _ in item.get("runs") or []:
            for old_first, old_last, old_index in old_runs_by_row.get(int(row), ()):
                overlap_by_index[old_index] += max(0, min(int(last), old_last) - max(int(first), old_first) + 1)
        overlapping = [(old[index], count) for index, count in overlap_by_index.items() if count > 0]
        if overlapping:
            matches = [candidate for candidate, _ in overlapping]
            for candidate, count in overlapping:
                link_details[(candidate["componentId"], item["componentId"])] = {
                    "linkBasis": "native_cell_overlap",
                    "overlapCells": count,
                    "boundingBoxGapCells": 0,
                    "cellGapCells": 0,
                }
        else:
            bounds = item["bounds"]
            possible_indices = set()
            for row in range(bounds["rowMin"], bounds["rowMax"] + 1):
                possible_indices.update(old_motion_by_row.get(row, ()))
            nearby = [(old[index], _component_gap(old[index], item, maximum_motion_cells)) for index in possible_indices]
            nearby = sorted(
                ((candidate, gap) for candidate, gap in nearby if gap <= maximum_motion_cells),
                key=lambda pair: (pair[1], -int(pair[0].get("cellCount") or 0), pair[0]["eventId"]),
            )
            matches = [nearby[0][0]] if nearby else []
            for candidate in matches:
                nearest = _component_gap(candidate, item, maximum_motion_cells)
                link_details[(candidate["componentId"], item["componentId"])] = {
                    "linkBasis": "nearest_motion_fallback",
                    "overlapCells": 0,
                    "boundingBoxGapCells": nearest,
                    "cellGapCells": nearest,
                }
        parents[item["componentId"]] = matches
        for match in matches:
            children[match["componentId"]].append(item)
        if matches:
            primary = sorted(
                matches,
                key=lambda match: (
                    -link_details[(match["componentId"], item["componentId"])]["overlapCells"],
                    link_details[(match["componentId"], item["componentId"])]["boundingBoxGapCells"],
                    -int(match.get("cellCount") or 0),
                    match["eventId"],
                ),
            )[0]
            item["eventId"] = primary["eventId"]
            item["lineageScanCount"] = int(primary.get("lineageScanCount") or 1) + 1
            item["primaryParentComponentId"] = primary["componentId"]
            item["parentComponentIds"] = sorted(match["componentId"] for match in matches)
            item["ancestorEventIds"] = sorted({
                ancestor
                for match in matches
                for ancestor in (match.get("ancestorEventIds") or [match["eventId"]])
            })
    edges = []
    for item in new:
        matches = parents[item["componentId"]]
        for match in matches:
            kind = "continuation"
            if len(matches) > 1:
                kind = "merge"
            elif len(children[match["componentId"]]) > 1:
                kind = "split"
            detail = link_details[(match["componentId"], item["componentId"])]
            edges.append({
                "fromComponentId": match["componentId"],
                "toComponentId": item["componentId"],
                "kind": kind,
                "isPrimary": match["componentId"] == item.get("primaryParentComponentId"),
                **detail,
            })
    current[edge_key] = sorted(edges, key=lambda item: (item["fromComponentId"], item["toComponentId"]))
    if event_key != "rainEvents":
        return current
    opportunity_events = {item["componentId"]: item for item in new}
    for opportunity in current.get("opportunities") or []:
        event = opportunity_events[opportunity["rainComponentId"]]
        opportunity["eventId"] = event["eventId"]
        opportunity["persistenceScans"] = int(event.get("lineageScanCount") or 1)
    return current


def build_storm_object_ledger(sidecar: dict, previous: dict | None = None) -> dict:
    objects = storm_objects(sidecar)
    ledger = {
        "schemaVersion": STORM_OBJECT_SCHEMA_VERSION,
        "methodVersion": STORM_OBJECT_METHOD_VERSION,
        "scanTime": str(sidecar["observedAt"]),
        "rainFootprintId": sidecar["rainFootprintId"],
        "stormObjects": objects,
        "stormLineageEdges": [],
        "stats": {
            "stormObjects": len(objects),
            "envelopeCells": sum(item["cellCount"] for item in objects),
            "coreCells": sum(item["coreCellCount"] for item in objects),
        },
    }
    return link_lineage(
        previous,
        ledger,
        event_key="stormObjects",
        edge_key="stormLineageEdges",
    )


def _component_cells(component: dict) -> set[tuple[int, int]]:
    return {(row, column) for row, first, last, _ in component["runs"] for column in range(first, last + 1)}


def _edge_cells(component: dict) -> list[tuple[int, int]]:
    cells = _component_cells(component)
    return sorted(cell for cell in cells if any((cell[0] + dr, cell[1] + dc) not in cells
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1))))


def apparent_solar_elevation(geometric_elevation_deg: float) -> float:
    elevation = float(geometric_elevation_deg)
    if elevation > 85:
        return elevation
    tangent = math.tan(math.radians(elevation))
    if elevation > 5:
        correction = 58.1 / tangent - 0.07 / tangent ** 3 + 0.000086 / tangent ** 5
    elif elevation > -0.575:
        correction = 1735 + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)))
    else:
        correction = -20.774 / tangent
    return elevation + correction / 3600


def _swath_cell(lat: float, lon: float) -> tuple[int, int]:
    west, south, _, _ = CONUS_BOUNDS
    return round((lat - south) / SWATH_RESOLUTION_DEG), round((lon - west) / SWATH_RESOLUTION_DEG)


def _swath_coordinate(row: int, column: int) -> tuple[float, float]:
    west, south, _, _ = CONUS_BOUNDS
    return south + row * SWATH_RESOLUTION_DEG, west + column * SWATH_RESOLUTION_DEG


@lru_cache(maxsize=1)
def _land_geometry():
    payload = json.loads(Path(__file__).with_name("conus-land.json").read_text(encoding="utf-8"))
    return shape(payload["geometry"])


def encode_swath(cells: set[tuple[int, int]]) -> list[list[int]]:
    rows: dict[int, list[int]] = defaultdict(list)
    for row, column in cells:
        rows[row].append(column)
    runs = []
    for row in sorted(rows):
        columns = sorted(set(rows[row]))
        start = previous = columns[0]
        for column in columns[1:]:
            if column != previous + 1:
                runs.append([row, start, previous])
                start = column
            previous = column
        runs.append([row, start, previous])
    return runs


def decode_swath(swath: dict) -> set[tuple[int, int]]:
    return {(int(row), column) for row, first, last in swath.get("runs") or [] for column in range(int(first), int(last) + 1)}


def swath_contains(swath: dict, lat: float, lon: float, tolerance_km: float = SWATH_TOLERANCE_KM) -> tuple[bool, float | None]:
    cells = decode_swath(swath)
    if not cells:
        return False, None
    target = _swath_cell(lat, lon)
    if target in cells:
        return True, 0.0
    best = min(math.hypot((row - target[0]) * SWATH_RESOLUTION_DEG * 111,
                          (column - target[1]) * SWATH_RESOLUTION_DEG * 111 * math.cos(math.radians(lat)))
               for row, column in cells)
    return best <= tolerance_km, round(best, 2)


def _representatives(cells: set[tuple[int, int]], spacing_km: float = REPRESENTATIVE_SPACING_KM) -> list[dict]:
    selected: list[tuple[float, float]] = []
    # Earth-centered buckets make the neighbor lookup safe across CONUS:
    # points closer than spacing_km cannot differ by more than one bucket in
    # any Cartesian axis. The exact legacy distance remains the final gate.
    buckets: dict[tuple[int, int, int], list[tuple[float, float]]] = defaultdict(list)
    radius_km = 6371.0

    def bucket(lat: float, lon: float) -> tuple[int, int, int]:
        latitude, longitude = math.radians(lat), math.radians(lon)
        x = radius_km * math.cos(latitude) * math.cos(longitude)
        y = radius_km * math.cos(latitude) * math.sin(longitude)
        z = radius_km * math.sin(latitude)
        return math.floor(x / spacing_km), math.floor(y / spacing_km), math.floor(z / spacing_km)

    for row, column in sorted(cells):
        lat, lon = _swath_coordinate(row, column)
        key = bucket(lat, lon)
        nearby = (
            point
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
            for point in buckets.get((key[0] + dx, key[1] + dy, key[2] + dz), ())
        )
        if any(math.hypot((lat - old_lat) * 111, (lon - old_lon) * 111 * math.cos(math.radians(lat))) < spacing_km
               for old_lat, old_lon in nearby):
            continue
        selected.append((lat, lon))
        buckets[key].append((lat, lon))
    return [{"lat": round(lat, 4), "lon": round(lon, 4), "derivedFromSwath": True} for lat, lon in selected]


def observer_swath(component: dict, sidecar: dict, observed_at: datetime) -> dict:
    grid = sidecar["grid"]
    lat0, dlat = float(grid["latitudeStart"]), float(grid["latitudeStepDeg"])
    lon0, dlon = float(grid["longitudeStart"]), float(grid["longitudeStepDeg"])
    edge_stride = max(1, round(RAIN_EDGE_SAMPLE_KM / max(0.5, abs(dlat) * 111)))
    edges = _edge_cells(component)[::edge_stride]
    projected_cells: set[tuple[int, int]] = set()
    if edges:
        rain_lats = np.asarray([lat0 + row * dlat for row, _ in edges], dtype=float)
        rain_lons = np.asarray([lon0 + column * dlon for _, column in edges], dtype=float)
        solar = [solar_position(observed_at, float(lat), float(lon)) for lat, lon in zip(rain_lats, rain_lons)]
        apparent = np.asarray([apparent_solar_elevation(item[0]) for item in solar])
        sun_bearings = np.asarray([item[1] for item in solar])
        valid_edges = (apparent >= -0.833) & (apparent < 42)
        rain_lats, rain_lons = rain_lats[valid_edges], rain_lons[valid_edges]
        apparent, sun_bearings = apparent[valid_edges], sun_bearings[valid_edges]
        if rain_lats.size:
            ratio = np.clip(np.cos(np.radians(42.0)) / np.cos(np.radians(apparent)), -1.0, 1.0)
            half_extents = np.degrees(np.arccos(ratio))
            angle_count = int(math.ceil(84.0 / ANGLE_STEP_DEG)) + 1
            steps = np.arange(angle_count, dtype=float) * ANGLE_STEP_DEG
            angles = -half_extents[:, None] + steps[None, :]
            valid_angles = angles <= half_extents[:, None] + 1e-9
            bearings = np.radians((sun_bearings[:, None] + angles) % 360.0)[:, :, None]
            distances = np.asarray(RAIN_DISTANCE_KM, dtype=float)[None, None, :] / 6371.0
            lat1 = np.radians(rain_lats)[:, None, None]
            lon1 = np.radians(rain_lons)[:, None, None]
            sin_lat1, cos_lat1 = np.sin(lat1), np.cos(lat1)
            sin_distance, cos_distance = np.sin(distances), np.cos(distances)
            lat2 = np.arcsin(sin_lat1 * cos_distance + cos_lat1 * sin_distance * np.cos(bearings))
            lon2 = lon1 + np.arctan2(
                np.sin(bearings) * sin_distance * cos_lat1,
                cos_distance - sin_lat1 * np.sin(lat2),
            )
            observer_lats, observer_lons = np.degrees(lat2), np.degrees(lon2)
            valid = np.broadcast_to(valid_angles[:, :, None], observer_lats.shape).copy()
            valid &= (
                (observer_lats >= CONUS_BOUNDS[1]) & (observer_lats <= CONUS_BOUNDS[3])
                & (observer_lons >= CONUS_BOUNDS[0]) & (observer_lons <= CONUS_BOUNDS[2])
            )
            rows = np.rint((observer_lats[valid] - CONUS_BOUNDS[1]) / SWATH_RESOLUTION_DEG).astype(int)
            columns = np.rint((observer_lons[valid] - CONUS_BOUNDS[0]) / SWATH_RESOLUTION_DEG).astype(int)
            projected_cells = set(zip(rows.tolist(), columns.tolist()))
    # Shapely applies the same land polygon to the complete unique-cell array
    # in native code. This replaces hundreds of thousands of Python ray casts.
    cells = set()
    if projected_cells:
        projected = np.asarray(sorted(projected_cells), dtype=int)
        cell_lats = CONUS_BOUNDS[1] + projected[:, 0] * SWATH_RESOLUTION_DEG
        cell_lons = CONUS_BOUNDS[0] + projected[:, 1] * SWATH_RESOLUTION_DEG
        on_land = np.asarray(contains_xy(_land_geometry(), cell_lons, cell_lats), dtype=bool)
        cells = {tuple(item) for item in projected[on_land].tolist()}
    runs = encode_swath(cells)
    return {
        "schemaVersion": "observer-swath.v1",
        "resolutionDeg": SWATH_RESOLUTION_DEG,
        "toleranceKm": SWATH_TOLERANCE_KM,
        "origin": {"lat": CONUS_BOUNDS[1], "lon": CONUS_BOUNDS[0]},
        "runs": runs,
        "cellCount": len(cells),
        "representativeCandidates": _representatives(cells),
    }


def camera_evidence_scope(distance_km: float, label: str) -> str:
    if float(distance_km) <= CAMERA_CANDIDATE_MAX_KM:
        return "candidate"
    return "event" if label == "rainbow" else "ignored"


def build_opportunity_ledger(sidecar: dict, previous: dict | None = None) -> dict:
    if not str(sidecar.get("schemaVersion", "")).startswith("mrms-rain-footprint.v"):
        raise ValueError("Opportunity Ledger requires an MRMS rain-footprint sidecar")
    observed_at = datetime.fromisoformat(str(sidecar["observedAt"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    events = rain_components(sidecar)
    opportunities = []
    for event in events:
        swath = observer_swath(event, sidecar, observed_at)
        if swath["cellCount"] <= 0:
            continue
        opportunities.append({
            "opportunityId": "bow-opportunity-" + event["componentId"].split("-")[-1],
            "rainComponentId": event["componentId"], "eventId": event["eventId"],
            "scanTime": _iso(observed_at), "observerSwath": swath,
            "persistenceScans": 1,
            "disposition": "retained_sensor_supported",
            "thresholdSnapshot": {
                "rainMinimumMmHr": 0.05, "sunApparentElevationDeg": [-0.833, 42],
                "rainDistanceKm": [min(RAIN_DISTANCE_KM), max(RAIN_DISTANCE_KM)],
                "angleStepDeg": ANGLE_STEP_DEG, "rainEdgeSampleKm": RAIN_EDGE_SAMPLE_KM,
                "projectionMethod": "vectorized-great-circle-v1",
                "terrainGate": False, "observerRainGate": False,
            },
        })
    ledger = {
        "schemaVersion": SCHEMA_VERSION, "methodVersion": METHOD_VERSION,
        "scanTime": _iso(observed_at), "rainFootprintId": sidecar["rainFootprintId"],
        "gridToleranceKm": SWATH_TOLERANCE_KM, "rainEvents": events,
        "opportunities": opportunities, "lineageEdges": [],
        "stats": {
            "rainEvents": len(events), "opportunities": len(opportunities),
            "observerSwathCells": sum(item["observerSwath"]["cellCount"] for item in opportunities),
            "representativeCandidates": sum(len(item["observerSwath"]["representativeCandidates"]) for item in opportunities),
        },
    }
    return link_lineage(previous, ledger)
