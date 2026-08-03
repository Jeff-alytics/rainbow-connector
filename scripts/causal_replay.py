"""Camera-independent, causal opportunity replay primitives.

This module is research-only. It consumes seed records retained before camera
matching, builds deterministic scan-by-scan lineages, scores several bounded
development models, assigns review lanes, and writes tamper-evident prediction
logs. It does not import or mutate production alert policy.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "causal-opportunity-replay.v2"
LINEAGE_VERSION = "camera-free-seed-lineage-nearest-v1"
REVIEW_POLICY_VERSION = "predict-then-review-2026-08-v1"
PREDICTION_LOG_VERSION = "tamper-evident-prediction-log.v1"

MODEL_VERSIONS = {
    "persistenceFirst": "causal-persistence-band-then-radar-v2",
    "boundedBlend": "causal-bounded-persistence-radar-v2",
    "radarFirst": "causal-radar-then-persistence-v2",
    "budgetGate": "causal-budget-gate-development-v2",
}

DEFAULT_CONFIG = {
    "maximumLineageGapMinutes": 30,
    "maximumObserverMotionKm": 45.0,
    "maximumRainTargetMotionKm": 60.0,
    "persistenceSaturationScans": 5,
    "budgetGateMinimumScans": 3,
    "budgetGateMinimumPeakRadar": 95.0,
    "budgetGateMinimumCurrentRadar": 70.0,
    "minimumWetNeighbors": 1,
    "primaryReviewMaximumRank": 5,
    "lowerRankControlMinimumRank": 6,
    "lowerRankControlMaximumRank": 50,
    "lowerRankControlFraction": 0.10,
    "blindFirstFraction": 0.12,
}


def canonical_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def content_sha256(value) -> str:
    raw = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def distance_km(lat1, lon1, lat2, lon2) -> float:
    values = [finite(item) for item in (lat1, lon1, lat2, lon2)]
    if any(item is None for item in values):
        return math.inf
    lat1, lon1, lat2, lon2 = values
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    term = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, term)))


def stable_fraction(key: str) -> float:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:13], 16) / float(16 ** 13)


def _candidate_id(stream_id: str, scan_time: str, seed: dict) -> str:
    identity = {
        "streamId": stream_id,
        "scanTime": scan_time,
        "lat": finite(seed.get("lat")),
        "lon": finite(seed.get("lon")),
        "rainLat": finite(seed.get("rainLat")),
        "rainLon": finite(seed.get("rainLon")),
    }
    return "seed-" + content_sha256(identity)[:20]


def _lineage_id(stream_id: str, candidate_id: str) -> str:
    return "opportunity-" + content_sha256({"streamId": stream_id, "firstCandidateId": candidate_id})[:20]


def _spatial_support(seed: dict, config: dict) -> tuple[str, bool | None, dict]:
    support = deepcopy(seed.get("spatialSupport") or {})
    isolated = support.get("isolatedPixel")
    neighbors = finite(
        support.get("adjacentWetCells")
        if support.get("adjacentWetCells") is not None
        else support.get("wetNeighborCount")
    )
    component_cells = finite(support.get("connectedWetCellCount"))
    if isolated is True or (neighbors is not None and neighbors < config["minimumWetNeighbors"]):
        return "failed", False, support
    if neighbors is not None or component_cells is not None:
        return "supported", True, support
    return "unknown", None, support


def _sunlight_modifier(seed: dict) -> tuple[float, str]:
    sunlight = seed.get("sunlightV2") or {}
    state = sunlight.get("sunlightState") or seed.get("sunlightState") or "unavailable"
    if state == "sunlit_supported":
        return 3.0, state
    if state == "overcast_supported" and sunlight.get("evidencePresent") is True:
        return -10.0, state
    return 0.0, state


def _validity(seed: dict, support_pass: bool | None) -> tuple[bool, list[str]]:
    reasons = []
    elevation = finite(seed.get("sunElevationDeg"))
    required = {
        "observer_location": finite(seed.get("lat")) is not None and finite(seed.get("lon")) is not None,
        "rain_location": finite(seed.get("rainLat")) is not None and finite(seed.get("rainLon")) is not None,
        "radar_score": finite(seed.get("radarScore")) is not None,
        "sun_geometry": elevation is not None and 0 <= elevation <= 30,
    }
    for name, passed in required.items():
        if not passed:
            reasons.append(name)
    observer_rain = finite(seed.get("observerRainRateMmHr"))
    if observer_rain is not None and observer_rain >= 0.1:
        reasons.append("observer_not_dry")
    if support_pass is False:
        reasons.append("radar_spatial_support")
    return not reasons, reasons


def _scores(state: dict, seed: dict, config: dict, valid: bool, support_pass: bool | None) -> dict:
    scans = int(state["scanCount"])
    saturation = max(1, int(config["persistenceSaturationScans"]))
    bounded_scans = min(scans, saturation)
    persistence = bounded_scans / saturation
    current = finite(seed.get("radarScore")) or 0.0
    peak = float(state["causalPeakRadar"])
    trend = max(-1.0, min(1.0, (current - float(state["previousRadar"])) / 20.0))
    sunlight_modifier, sunlight_state = _sunlight_modifier(seed)
    balanced = max(0.0, min(100.0,
        100 * (0.45 * peak / 100 + 0.35 * persistence + 0.15 * current / 100 + 0.05 * (trend + 1) / 2)
        + sunlight_modifier))
    radar_first = max(0.0, min(100.0, 75 * peak / 100 + 25 * persistence + sunlight_modifier))
    persistence_first = bounded_scans * 1000 + peak
    budget_eligible = (
        valid
        and scans >= int(config["budgetGateMinimumScans"])
        and peak >= float(config["budgetGateMinimumPeakRadar"])
        and current >= float(config["budgetGateMinimumCurrentRadar"])
        and support_pass is not False
    )
    features = {
        "boundedPersistenceScans": bounded_scans,
        "persistenceSaturated": scans >= saturation,
        "causalPeakRadar": round(peak, 3),
        "currentRadar": round(current, 3),
        "sunlightState": sunlight_state,
        "sunlightModifier": sunlight_modifier,
    }
    models = {
        "persistenceFirst": {"score": round(persistence_first, 3), "eligible": valid},
        "boundedBlend": {"score": round(balanced, 3), "eligible": valid},
        "radarFirst": {"score": round(radar_first, 3), "eligible": valid},
        "budgetGate": {"score": round(balanced, 3), "eligible": budget_eligible,
                       "developmentCandidate": True},
    }
    return features, models


class CausalReplay:
    def __init__(self, config: dict | None = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.lineages: dict[str, dict] = {}
        self.active_lineage_ids: set[str] = set()
        self.review_assignments: dict[str, dict] = {}

    def _match(self, stream_id: str, scan_at: datetime, seeds: list[dict]) -> dict[int, tuple[str, float, float]]:
        maximum_gap = self.config["maximumLineageGapMinutes"]
        self.active_lineage_ids = {
            lineage_id for lineage_id in self.active_lineage_ids
            if 0 < (scan_at - self.lineages[lineage_id]["lastSeen"]).total_seconds() / 60 <= maximum_gap
        }
        buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
        for lineage_id in self.active_lineage_ids:
            state = self.lineages[lineage_id]
            if state["streamId"] == stream_id:
                buckets[(math.floor(state["lat"]), math.floor(state["lon"]))].append(lineage_id)
        candidates = []
        for index, seed in enumerate(seeds):
            lat, lon = finite(seed.get("lat")), finite(seed.get("lon"))
            if lat is None or lon is None:
                nearby = []
            else:
                nearby = [
                    lineage_id
                    for row in range(math.floor(lat) - 1, math.floor(lat) + 2)
                    for column in range(math.floor(lon) - 1, math.floor(lon) + 2)
                    for lineage_id in buckets.get((row, column), [])
                ]
            for lineage_id in nearby:
                state = self.lineages[lineage_id]
                gap = (scan_at - state["lastSeen"]).total_seconds() / 60
                observer_distance = distance_km(seed.get("lat"), seed.get("lon"),
                                                state["lat"], state["lon"])
                rain_distance = distance_km(seed.get("rainLat"), seed.get("rainLon"),
                                            state["rainLat"], state["rainLon"])
                if observer_distance > self.config["maximumObserverMotionKm"]:
                    continue
                if rain_distance > self.config["maximumRainTargetMotionKm"]:
                    continue
                cost = observer_distance + 0.35 * rain_distance + 0.2 * gap
                candidates.append((round(cost, 6), lineage_id, index, observer_distance, rain_distance))
        matched_indices, matched_lineages, matches = set(), set(), {}
        for _, lineage_id, index, observer_distance, rain_distance in sorted(candidates):
            if index in matched_indices or lineage_id in matched_lineages:
                continue
            matched_indices.add(index)
            matched_lineages.add(lineage_id)
            matches[index] = (lineage_id, observer_distance, rain_distance)
        return matches

    def process_scan(self, stream_id: str, scan_time: str, seeds: Iterable[dict]) -> dict:
        scan_at = parse_utc(scan_time)
        normalized = [deepcopy(seed) for seed in seeds]
        normalized.sort(key=lambda seed: _candidate_id(stream_id, scan_time, seed))
        matches = self._match(stream_id, scan_at, normalized)
        candidates = []
        for index, seed in enumerate(normalized):
            candidate_id = _candidate_id(stream_id, scan_time, seed)
            if index in matches:
                lineage_id, observer_motion, rain_motion = matches[index]
                state = self.lineages[lineage_id]
                previous_radar = finite(state.get("currentRadar")) or 0.0
                state["scanCount"] += 1
                state["lastSeen"] = scan_at
                state["lat"], state["lon"] = finite(seed.get("lat")), finite(seed.get("lon"))
                state["rainLat"], state["rainLon"] = finite(seed.get("rainLat")), finite(seed.get("rainLon"))
                state["currentRadar"] = finite(seed.get("radarScore")) or 0.0
                state["causalPeakRadar"] = max(float(state["causalPeakRadar"]), state["currentRadar"])
            else:
                lineage_id = _lineage_id(stream_id, candidate_id)
                observer_motion = rain_motion = None
                previous_radar = finite(seed.get("radarScore")) or 0.0
                state = {
                    "lineageId": lineage_id, "streamId": stream_id, "scanCount": 1,
                    "firstSeen": scan_at, "lastSeen": scan_at,
                    "lat": finite(seed.get("lat")), "lon": finite(seed.get("lon")),
                    "rainLat": finite(seed.get("rainLat")), "rainLon": finite(seed.get("rainLon")),
                    "currentRadar": finite(seed.get("radarScore")) or 0.0,
                    "causalPeakRadar": finite(seed.get("radarScore")) or 0.0,
                }
                self.lineages[lineage_id] = state
                self.active_lineage_ids.add(lineage_id)
            state["previousRadar"] = previous_radar
            support_state, support_pass, support = _spatial_support(seed, self.config)
            valid, validity_reasons = _validity(seed, support_pass)
            causal_features, models = _scores(state, seed, self.config, valid, support_pass)
            candidates.append({
                "candidateId": candidate_id,
                "lineageId": lineage_id,
                "scanCount": state["scanCount"],
                "firstSeenAt": iso_utc(state["firstSeen"]),
                "lat": state["lat"], "lon": state["lon"],
                "rainLat": state["rainLat"], "rainLon": state["rainLon"],
                "radarScore": state["currentRadar"],
                "causalPeakRadar": state["causalPeakRadar"],
                "lineageMatch": {
                    "version": LINEAGE_VERSION,
                    "observerMotionKm": round(observer_motion, 3) if observer_motion is not None else None,
                    "rainTargetMotionKm": round(rain_motion, 3) if rain_motion is not None else None,
                },
                "spatialSupportState": support_state,
                "spatialSupport": support,
                "validity": {"eligible": valid, "reasons": validity_reasons},
                "causalFeatures": causal_features,
                "models": models,
            })
        self._rank(candidates)
        self._assign_review_lanes(candidates)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "streamId": stream_id,
            "scanTime": iso_utc(scan_at),
            "lineageVersion": LINEAGE_VERSION,
            "modelVersions": MODEL_VERSIONS,
            "candidates": sorted(candidates, key=lambda item: item["candidateId"]),
        }

    def _rank(self, candidates: list[dict]) -> None:
        for model_name in MODEL_VERSIONS:
            ordered = sorted(
                candidates,
                key=lambda item: (
                    not bool(item["models"][model_name]["eligible"]),
                    -float(item["models"][model_name]["score"]),
                    item["lineageId"],
                ),
            )
            for rank, item in enumerate(ordered, 1):
                item["models"][model_name]["rank"] = rank

    def _assign_review_lanes(self, candidates: list[dict]) -> None:
        model = "boundedBlend"
        for item in sorted(candidates, key=lambda candidate: candidate["models"][model]["rank"]):
            lineage_id = item["lineageId"]
            if lineage_id not in self.review_assignments and item["scanCount"] >= 2:
                rank = item["models"][model]["rank"]
                lane = None
                if rank <= self.config["primaryReviewMaximumRank"]:
                    lane = "primary"
                elif (
                    self.config["lowerRankControlMinimumRank"] <= rank
                    <= self.config["lowerRankControlMaximumRank"]
                    and stable_fraction(lineage_id + ":" + REVIEW_POLICY_VERSION + ":control")
                    < self.config["lowerRankControlFraction"]
                ):
                    lane = "lower_rank_control"
                if lane:
                    self.review_assignments[lineage_id] = {
                        "lane": lane,
                        "blindFirst": stable_fraction(lineage_id + ":" + REVIEW_POLICY_VERSION + ":blind")
                        < self.config["blindFirstFraction"],
                        "policyVersion": REVIEW_POLICY_VERSION,
                        "lockedAtScanCount": item["scanCount"],
                    }
            item["reviewPolicy"] = deepcopy(self.review_assignments.get(lineage_id))


def streams_from_camera_free_artifact(payload: dict) -> list[dict]:
    if payload.get("schemaVersion") != "frozen-case-camera-free-check.v1":
        raise ValueError("Expected frozen-case-camera-free-check.v1 input")
    streams = []
    for case in payload.get("cases") or []:
        streams.append({
            "streamId": case["id"],
            "window": case.get("window"),
            "observations": case.get("observations") or [],
            "inputScope": "camera_independent_case_radius",
            "scans": [
                {"scanTime": scan["scanTime"], "seeds": scan.get("seedsWithinRadius") or []}
                for scan in case.get("scans") or []
            ],
        })
    return streams


def streams_from_pool_files(paths: Iterable[Path], allow_camera_filtered: bool = False) -> list[dict]:
    scans = []
    scope = "camera_independent_nationwide"
    for path in sorted(Path(item) for item in paths):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") != "ok":
                continue
            if "allSeeds" in row:
                seeds = row["allSeeds"]
            elif allow_camera_filtered:
                seeds = row.get("matched") or []
                scope = "camera_filtered_legacy"
            else:
                raise ValueError(
                    f"{path} does not retain allSeeds; rerun the pool builder with --retain-all-seeds"
                )
            scans.append({"scanTime": row["observedAt"], "seeds": seeds})
    scans.sort(key=lambda item: item["scanTime"])
    return [{"streamId": "nationwide", "inputScope": scope, "scans": scans}]


def iter_pool_scans(paths: Iterable[Path], allow_camera_filtered: bool = False):
    """Merge sharded JSONL pools chronologically without loading the month."""
    scope = {"value": "camera_independent_nationwide"}

    def rows(path: Path):
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("status") != "ok":
                    continue
                if "allSeeds" in row:
                    seeds = row["allSeeds"]
                elif allow_camera_filtered:
                    seeds = row.get("matched") or []
                    scope["value"] = "camera_filtered_legacy"
                else:
                    raise ValueError(
                        f"{path}:{line_number} does not retain allSeeds; "
                        "rerun the pool builder with --retain-all-seeds"
                    )
                yield row["observedAt"], str(path), {
                    "scanTime": row["observedAt"], "seeds": seeds,
                }

    generators = [rows(Path(path)) for path in sorted(Path(item) for item in paths)]
    for _, _, scan in heapq.merge(*generators):
        yield scan, scope


def run_streams(streams: list[dict], config: dict | None = None) -> dict:
    records, summaries = [], []
    for stream in streams:
        replay = CausalReplay(config)
        stream_records = [
            replay.process_scan(stream["streamId"], scan["scanTime"], scan.get("seeds") or [])
            for scan in sorted(stream.get("scans") or [], key=lambda item: item["scanTime"])
        ]
        records.extend(stream_records)
        summary = {
            "streamId": stream["streamId"],
            "inputScope": stream.get("inputScope"),
            "scans": len(stream_records),
            "lineages": len(replay.lineages),
            "window": stream.get("window"),
            "models": {},
        }
        window_start = parse_utc(stream["window"][0]) if stream.get("window") else None
        for model_name in MODEL_VERSIONS:
            eligible = [
                (record["scanTime"], candidate)
                for record in stream_records for candidate in record["candidates"]
                if candidate["models"][model_name]["eligible"]
            ]
            first = min(eligible, key=lambda item: item[0]) if eligible else None
            summary["models"][model_name] = {
                "firstEligibleAt": first[0] if first else None,
                "firstEligibleLineageId": first[1]["lineageId"] if first else None,
                "leadMinutesToWindowStart": round(
                    (window_start - parse_utc(first[0])).total_seconds() / 60, 1
                ) if first and window_start else None,
            }
        summaries.append(summary)
    records.sort(key=lambda item: (item["scanTime"], item["streamId"]))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedFromCausalInputsOnly": True,
        "config": {**DEFAULT_CONFIG, **(config or {})},
        "modelVersions": MODEL_VERSIONS,
        "streams": summaries,
        "records": records,
    }


def write_prediction_package(result: dict, output_dir: Path, source_paths: Iterable[Path]) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for record in result["records"]:
        by_day[record["scanTime"][:10]].append(record)
    files = []
    for day, records in sorted(by_day.items()):
        path = output_dir / f"predictions-{day}.jsonl"
        previous_hash = "0" * 64
        lines = []
        for sequence, record in enumerate(records, 1):
            payload = {"sequence": sequence, "previousHash": previous_hash, "prediction": record}
            record_hash = content_sha256(payload)
            line = {**payload, "recordHash": record_hash}
            lines.append(canonical_json(line))
            previous_hash = record_hash
        raw = (chr(10).join(lines) + chr(10)).encode("utf-8")
        with path.open("xb") as stream:
            stream.write(raw)
        files.append({
            "path": path.name,
            "records": len(records),
            "contentSha256": hashlib.sha256(raw).hexdigest(),
            "finalRecordHash": previous_hash,
        })
    sources = []
    for path in sorted(Path(item) for item in source_paths):
        sources.append({"path": str(path), "contentSha256": file_sha256(path)})
    manifest = {
        "schemaVersion": PREDICTION_LOG_VERSION,
        "replaySchemaVersion": result["schemaVersion"],
        "lineageVersion": LINEAGE_VERSION,
        "modelVersions": MODEL_VERSIONS,
        "reviewPolicyVersion": REVIEW_POLICY_VERSION,
        "config": result["config"],
        "sources": sources,
        "files": files,
        "streamSummaries": result["streams"],
        "gitCommitInstruction": "Commit this manifest hash before human review; labels live in a separate artifact.",
    }
    manifest["manifestContentSha256"] = content_sha256(manifest)
    manifest_path = output_dir / "prediction-manifest.json"
    with manifest_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(manifest, indent=2, ensure_ascii=False) + chr(10))
    return manifest


def stream_pool_to_prediction_package(
    pool_paths: Iterable[Path],
    output_dir: Path,
    source_paths: Iterable[Path] | None = None,
    config: dict | None = None,
    allow_camera_filtered: bool = False,
) -> tuple[dict, dict]:
    """Replay and hash a nationwide pool incrementally with bounded memory."""
    pool_paths = sorted(Path(path) for path in pool_paths)
    if not pool_paths:
        raise ValueError("No pool JSONL files supplied")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError(f"Prediction output directory must be empty: {output_dir}")

    replay = CausalReplay(config)
    current_day = None
    stream = None
    digest = None
    sequence = 0
    previous_hash = "0" * 64
    files = []
    scans = 0
    first_eligible = {model: None for model in MODEL_VERSIONS}
    scope_value = "camera_independent_nationwide"

    def close_day():
        nonlocal stream
        if stream is None:
            return
        stream.close()
        files.append({
            "path": f"predictions-{current_day}.jsonl",
            "records": sequence,
            "contentSha256": digest.hexdigest(),
            "finalRecordHash": previous_hash,
        })
        stream = None

    for scan, scope in iter_pool_scans(pool_paths, allow_camera_filtered):
        scope_value = scope["value"]
        record = replay.process_scan("nationwide", scan["scanTime"], scan["seeds"])
        day = record["scanTime"][:10]
        if day != current_day:
            close_day()
            current_day = day
            sequence = 0
            previous_hash = "0" * 64
            digest = hashlib.sha256()
            stream = (output_dir / f"predictions-{day}.jsonl").open("xb")
        sequence += 1
        payload = {"sequence": sequence, "previousHash": previous_hash, "prediction": record}
        record_hash = content_sha256(payload)
        raw = (canonical_json({**payload, "recordHash": record_hash}) + chr(10)).encode("utf-8")
        stream.write(raw)
        digest.update(raw)
        previous_hash = record_hash
        scans += 1
        for model in MODEL_VERSIONS:
            if first_eligible[model] is None:
                candidate = next(
                    (item for item in record["candidates"] if item["models"][model]["eligible"]),
                    None,
                )
                if candidate:
                    first_eligible[model] = {
                        "firstEligibleAt": record["scanTime"],
                        "firstEligibleLineageId": candidate["lineageId"],
                        "leadMinutesToWindowStart": None,
                    }
    close_day()
    sources = [
        {"path": str(path), "contentSha256": file_sha256(path)}
        for path in sorted(Path(item) for item in (source_paths or pool_paths))
    ]
    summary = {
        "streamId": "nationwide", "inputScope": scope_value, "scans": scans,
        "lineages": len(replay.lineages), "window": None,
        "models": {
            model: first_eligible[model] or {
                "firstEligibleAt": None, "firstEligibleLineageId": None,
                "leadMinutesToWindowStart": None,
            }
            for model in MODEL_VERSIONS
        },
    }
    manifest = {
        "schemaVersion": PREDICTION_LOG_VERSION,
        "replaySchemaVersion": SCHEMA_VERSION,
        "lineageVersion": LINEAGE_VERSION,
        "modelVersions": MODEL_VERSIONS,
        "reviewPolicyVersion": REVIEW_POLICY_VERSION,
        "config": {**DEFAULT_CONFIG, **(config or {})},
        "sources": sources,
        "files": files,
        "streamSummaries": [summary],
        "streaming": True,
        "gitCommitInstruction": "Commit this manifest hash before human review; labels live in a separate artifact.",
    }
    manifest["manifestContentSha256"] = content_sha256(manifest)
    (output_dir / "prediction-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8"
    )
    return manifest, summary


def verify_prediction_package(output_dir: Path) -> dict:
    output_dir = Path(output_dir)
    manifest_path = output_dir / "prediction-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"ok": False, "errors": [f"invalid prediction manifest: {error}"]}
    expected_manifest_hash = manifest.pop("manifestContentSha256", None)
    errors = []
    if content_sha256(manifest) != expected_manifest_hash:
        errors.append("manifest hash mismatch")
    for source in manifest.get("sources") or []:
        path = Path(source["path"])
        if not path.exists():
            errors.append(f"missing source file: {path}")
        elif file_sha256(path) != source.get("contentSha256"):
            errors.append(f"source hash mismatch: {path}")
    for file_record in manifest.get("files") or []:
        path = output_dir / file_record["path"]
        if not path.exists():
            errors.append(f"missing prediction file: {path.name}")
            continue
        digest = hashlib.sha256()
        previous_hash = "0" * 64
        count = 0
        with path.open("rb") as stream:
            for count, raw_line in enumerate(stream, 1):
                digest.update(raw_line)
                try:
                    item = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    errors.append(f"invalid prediction record: {path.name}:{count}: {error}")
                    break
                record_hash = item.pop("recordHash", None)
                if item.get("sequence") != count or item.get("previousHash") != previous_hash:
                    errors.append(f"chain sequence mismatch: {path.name}:{count}")
                    break
                if content_sha256(item) != record_hash:
                    errors.append(f"record hash mismatch: {path.name}:{count}")
                    break
                previous_hash = record_hash
        if digest.hexdigest() != file_record["contentSha256"]:
            errors.append(f"content hash mismatch: {path.name}")
            continue
        if count != file_record["records"] or previous_hash != file_record["finalRecordHash"]:
            errors.append(f"chain terminus mismatch: {path.name}")
    return {"ok": not errors, "errors": errors}
