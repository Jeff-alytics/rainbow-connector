"""Non-operational sunlight-v2 features collected after live publication."""

from __future__ import annotations

import csv
import gzip
import math
import re
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any

import numpy as np
import requests
import xarray as xr

from decision_log import SUNLIGHT_V2_METHOD_VERSION, candidate_id, dsrf_feature
from detector_core import offset, solar_position
from goes_sample import (
    AWS_BUCKETS, DEFAULT_PRODUCT, FALLBACK_PRODUCT, USER_AGENT, _DATASET_CACHE,
    download_key, latlon_to_scan_xy, nearest_index, object_creation_from_key,
    projection_attrs, sample, scan_start_from_key, satellite_for,
)

METHOD_VERSION = SUNLIGHT_V2_METHOD_VERSION
BAND2_PRODUCT = "ABI-L1b-RadC"
BAND2_CHANNEL_TOKEN = "M6C02"
BAND2_NORMALIZATION_VERSION = "abi-kappa0-divide-cos-sza-v1"
BAND2_GAP_THRESHOLD = 0.35
BAND2_MIN_SUN_ELEVATION_DEG = 2.0
BAND2_BOX_PIXELS = 10
BAND2_MIN_VALID_PIXELS = 80
BAND2_LOOKBACK_MINUTES = 15
METAR_URL = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
METAR_METHOD_VERSION = "aviationweather-current-cache-v1"
DSRF_CLEAR_SKY_METHOD_VERSION = "haurwitz-1945-v1"


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def angular_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlon = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


def parse_metar_cache(payload: bytes) -> list[dict]:
    text = gzip.decompress(payload).decode("utf-8", errors="replace")
    rows = csv.reader(StringIO(text))
    header = next(rows)
    index = {name: header.index(name) for name in (
        "raw_text", "station_id", "observation_time", "latitude", "longitude",
        "visibility_statute_mi", "wx_string", "flight_category",
    )}
    sky_indices = [position for position, name in enumerate(header) if name == "sky_cover"]
    base_indices = [position for position, name in enumerate(header) if name == "cloud_base_ft_agl"]
    observations = []
    for row in rows:
        try:
            lat, lon = float(row[index["latitude"]]), float(row[index["longitude"]])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == -99.99 and lon == -99.99):
                continue
            layers = []
            for sky_index, base_index in zip(sky_indices, base_indices):
                cover = row[sky_index].strip() if sky_index < len(row) else ""
                base = row[base_index].strip() if base_index < len(row) else ""
                if cover:
                    layers.append({"cover": cover, "baseFtAgl": float(base) if base else None})
            observations.append({
                "stationId": row[index["station_id"]], "observedAt": row[index["observation_time"]],
                "lat": lat, "lon": lon, "visibilityStatuteMi": row[index["visibility_statute_mi"]] or None,
                "weather": row[index["wx_string"]] or None, "flightCategory": row[index["flight_category"]] or None,
                "cloudLayers": layers, "rawText": row[index["raw_text"]],
            })
        except (ValueError, IndexError):
            continue
    return observations


def fetch_metars(session: requests.Session | None = None) -> list[dict]:
    session = session or requests.Session()
    response = session.get(METAR_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return parse_metar_cache(response.content)


def metar_compatibility(station: dict) -> float:
    covers = {str(layer.get("cover") or "").upper() for layer in station.get("cloudLayers") or []}
    raw = str(station.get("rawText") or "").upper()
    if not covers and (" CLR" in raw or " SKC" in raw):
        return 0.9
    if covers & {"FEW", "SCT"}:
        return 0.75
    if covers & {"BKN", "OVC", "OVX"}:
        return 0.3
    return 0.5


def nearest_metars(
    candidate: dict,
    observations: list[dict],
    observed_at: str,
    available_by: str | None = None,
    maximum: int = 3,
    method_version: str = METAR_METHOD_VERSION,
) -> dict:
    event_time = parse_utc(observed_at)
    cutoff = parse_utc(available_by) if available_by else None
    nearby = []
    excluded_after_cutoff = 0
    for station in observations:
        distance = angular_distance_km(candidate["lat"], candidate["lon"], station["lat"], station["lon"])
        if distance > 30:
            continue
        try:
            station_time = parse_utc(station["observedAt"])
            offset_minutes = (station_time - event_time).total_seconds() / 60
        except (TypeError, ValueError):
            station_time = None
            offset_minutes = None
        if cutoff is not None and (station_time is None or station_time > cutoff):
            excluded_after_cutoff += 1
            continue
        nearby.append({
            **{name: station.get(name) for name in ("stationId", "observedAt", "visibilityStatuteMi", "weather", "flightCategory", "cloudLayers")},
            "distanceKm": round(distance, 2), "timeOffsetMinutes": None if offset_minutes is None else round(offset_minutes, 1),
            "sunShowerCompatibility": metar_compatibility(station), "causalEligible": cutoff is not None,
        })
    nearby.sort(key=lambda item: (item["distanceKm"], abs(item["timeOffsetMinutes"] or 999)))
    selected = nearby[:maximum]
    return {
        "available": bool(selected), "methodVersion": method_version,
        "availableBy": available_by, "excludedAfterCutoff": excluded_after_cutoff,
        "stations": selected, "supportScore": max((item["sunShowerCompatibility"] for item in selected), default=None),
    }


def dataset_for_raw(raw: dict) -> xr.Dataset:
    path = str(raw["cachePath"])
    dataset = _DATASET_CACHE.get(path)
    if dataset is None:
        dataset = xr.open_dataset(path, mask_and_scale=True)
        _DATASET_CACHE[path] = dataset
    return dataset


def acmc_neighborhood(raw: dict, radius_pixels: int = 2) -> dict:
    dataset = dataset_for_raw(raw)
    ix, iy = int(raw["pixel"]["x"]), int(raw["pixel"]["y"])
    values = np.asarray(dataset["ACM"].isel(
        x=slice(max(0, ix - radius_pixels), ix + radius_pixels + 1),
        y=slice(max(0, iy - radius_pixels), iy + radius_pixels + 1),
    ).values).reshape(-1)
    values = values[np.isfinite(values)].astype(int)
    counts = {name: int(np.count_nonzero(values == code)) for code, name in enumerate(("clear", "probablyClear", "probablyCloudy", "cloudy"))}
    total = int(values.size)
    fractions = {name: (count / total if total else None) for name, count in counts.items()}
    support = None if not total else fractions["clear"] + 0.7 * fractions["probablyClear"] + 0.3 * fractions["probablyCloudy"]
    return {
        "available": bool(total), "observedAt": raw.get("observedAt"), "createdAt": raw.get("createdAt"),
        "causalAvailableBy": raw.get("causalAvailableBy"), "causalEligible": raw.get("causalEligible"),
        "sourceKey": raw.get("s3Key"), "radiusPixels": radius_pixels,
        "counts": counts, "fractions": fractions, "supportScore": support,
    }


def haurwitz_clear_sky_wm2(sun_elevation_deg: float) -> float | None:
    cosine_zenith = math.sin(math.radians(sun_elevation_deg))
    if cosine_zenith <= 0:
        return None
    return 1098.0 * cosine_zenith * math.exp(-0.059 / cosine_zenith)


def dsrf_shadow(raw: dict, sun_elevation_deg: float) -> dict:
    feature = dsrf_feature(raw, sun_elevation_deg)
    expected = haurwitz_clear_sky_wm2(sun_elevation_deg) if feature.get("usable") else None
    value = feature.get("valueWm2")
    ratio = None if expected is None or expected < 20 or value is None else max(0.0, float(value) / expected)
    return {
        **feature, "createdAt": raw.get("createdAt"), "causalAvailableBy": raw.get("causalAvailableBy"),
        "causalEligible": raw.get("causalEligible"), "sourceKey": raw.get("s3Key"),
        "clearSkyExpectedWm2": expected, "clearnessRatio": ratio,
        "clearSkyMethodVersion": DSRF_CLEAR_SKY_METHOD_VERSION if expected is not None else None,
    }


def list_band2_keys(
    bucket: str,
    observed_at: datetime,
    session: requests.Session,
    available_by: datetime | None = None,
) -> list[str]:
    keys = []
    through = available_by or observed_at
    hours = {observed_at.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)}
    cursor = observed_at.replace(minute=0, second=0, microsecond=0)
    while cursor <= through.replace(minute=0, second=0, microsecond=0):
        hours.add(cursor)
        cursor += timedelta(hours=1)
    for moment in sorted(hours):
        prefix = f"{BAND2_PRODUCT}/{moment.year}/{moment.timetuple().tm_yday:03d}/{moment.hour:02d}/"
        response = session.get(f"https://{bucket}.s3.amazonaws.com/", params={"list-type": "2", "prefix": prefix}, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
        keys.extend(key for key in re.findall(r"<Key>(.*?)</Key>", response.text) if BAND2_CHANNEL_TOKEN in key and key.endswith(".nc"))
    return keys


class Band2Sampler:
    def __init__(
        self,
        observed_at: str,
        cache_dir: Path,
        session: requests.Session | None = None,
        available_by: str | None = None,
    ):
        self.observed_at = parse_utc(observed_at)
        self.available_by = parse_utc(available_by) if available_by else None
        self.cache_dir = cache_dir
        self.session = session or requests.Session()
        self.datasets: dict[str, list[tuple[dict, xr.Dataset]] | tuple[dict, xr.Dataset]] = {}

    def load_frames(self, satellite_position: str) -> list[tuple[dict, xr.Dataset]]:
        if satellite_position in self.datasets:
            stored = self.datasets[satellite_position]
            return list(stored) if isinstance(stored, list) else [stored]
        bucket = AWS_BUCKETS[satellite_position]
        choices = []
        through = self.available_by or self.observed_at + timedelta(minutes=BAND2_LOOKBACK_MINUTES)
        for key in list_band2_keys(bucket, self.observed_at, self.session, through):
            stamp = scan_start_from_key(key)
            if not stamp:
                continue
            frame_time = parse_utc(stamp)
            created_at = object_creation_from_key(key)
            created_time = parse_utc(created_at) if created_at else None
            if frame_time < self.observed_at - timedelta(minutes=BAND2_LOOKBACK_MINUTES) or frame_time > through:
                continue
            if self.available_by is not None and (created_time is None or created_time > self.available_by):
                continue
            choices.append((frame_time, key, stamp, created_at))
        if not choices:
            raise RuntimeError("No causally available Band-2 frame in the trailing window")
        loaded = []
        for frame_time, key, stamp, created_at in sorted(choices):
            path = download_key(bucket, key, self.cache_dir, session=self.session)
            metadata = {
                "product": BAND2_PRODUCT, "channel": 2, "s3Key": key, "observedAt": stamp,
                "createdAt": created_at,
                "timeOffsetMinutes": round((frame_time - self.observed_at).total_seconds() / 60, 1),
                "assessmentOffsetMinutes": None if self.available_by is None else round((frame_time - self.available_by).total_seconds() / 60, 1),
                "causalAvailableBy": None if self.available_by is None else self.available_by.isoformat().replace("+00:00", "Z"),
                "causalEligible": self.available_by is not None,
                "satellitePosition": satellite_position,
            }
            loaded.append((metadata, xr.open_dataset(path, mask_and_scale=True)))
        self.datasets[satellite_position] = loaded
        return loaded

    def box_stats(self, dataset: xr.Dataset, lat: float, lon: float, sun_elevation_deg: float) -> dict:
        x, y = latlon_to_scan_xy(lat, lon, projection_attrs(dataset))
        ix, iy = nearest_index(dataset["x"].values, x), nearest_index(dataset["y"].values, y)
        x0, y0 = max(0, ix - BAND2_BOX_PIXELS // 2), max(0, iy - BAND2_BOX_PIXELS // 2)
        x1, y1 = min(dataset.sizes["x"], x0 + BAND2_BOX_PIXELS), min(dataset.sizes["y"], y0 + BAND2_BOX_PIXELS)
        radiance = np.asarray(dataset["Rad"].isel(x=slice(x0, x1), y=slice(y0, y1)).values, dtype=float).reshape(-1)
        dqf = np.asarray(dataset["DQF"].isel(x=slice(x0, x1), y=slice(y0, y1)).values).reshape(-1)
        kappa0 = float(np.asarray(dataset["kappa0"].values).reshape(-1)[0])
        valid = np.isfinite(radiance) & (dqf == 0)
        factor = radiance[valid] * kappa0
        cosine_zenith = math.sin(math.radians(sun_elevation_deg))
        enough_pixels = int(valid.sum()) >= BAND2_MIN_VALID_PIXELS
        valid_sun = sun_elevation_deg >= BAND2_MIN_SUN_ELEVATION_DEG and cosine_zenith > 0.02
        normalized = factor / cosine_zenith if enough_pixels and valid_sun else np.array([])
        return {
            "available": bool(normalized.size), "pixel": {"x": ix, "y": iy}, "boxPixels": BAND2_BOX_PIXELS,
            "validPixelCount": int(valid.sum()), "minimumValidPixelCount": BAND2_MIN_VALID_PIXELS,
            "kappa0": kappa0, "validPixelFraction": float(valid.mean()) if valid.size else None,
            "unavailableReason": None if normalized.size else "sun_below_band2_limit" if not valid_sun else "insufficient_valid_pixels",
            "reflectanceFactorMean": float(factor.mean()) if factor.size else None,
            "cosineNormalizedMean": float(normalized.mean()) if normalized.size else None,
            "gapFraction": float(np.mean(normalized <= BAND2_GAP_THRESHOLD)) if normalized.size else None,
        }

    def sample_frame(self, candidate: dict, metadata: dict, dataset: xr.Dataset) -> dict:
        elevation, bearing = solar_position(parse_utc(metadata["observedAt"]), candidate["lat"], candidate["lon"])
        scenarios = {"observer": self.box_stats(dataset, candidate["lat"], candidate["lon"], elevation)}
        if elevation >= BAND2_MIN_SUN_ELEVATION_DEG:
            for height_km in (2, 4, 6):
                distance = min(120.0, height_km / max(0.02, math.tan(math.radians(elevation))))
                lat, lon = offset(candidate["lat"], candidate["lon"], distance, bearing)
                scenarios[f"sunwardCloud{height_km}Km"] = {"displacementKm": round(distance, 2), **self.box_stats(dataset, lat, lon, elevation)}
        gap_values = [value["gapFraction"] for value in scenarios.values() if value.get("gapFraction") is not None]
        return {
            **metadata, "sunElevationDeg": round(elevation, 3), "sunBearingDeg": round(bearing, 3),
            "available": bool(gap_values), "scenarios": scenarios,
            "maxGapFraction": max(gap_values) if gap_values else None,
        }

    def sample_candidate(self, candidate: dict) -> dict:
        satellite_position = satellite_for(candidate["lon"], "auto")
        frames = [self.sample_frame(candidate, metadata, dataset) for metadata, dataset in self.load_frames(satellite_position)]
        values = [frame["maxGapFraction"] for frame in frames if frame.get("maxGapFraction") is not None]
        temporal_value = float(np.percentile(values, 75)) if values else None
        closest = min(frames, key=lambda frame: abs(float(frame.get("timeOffsetMinutes") or 0)))
        return {
            "available": bool(values), "product": BAND2_PRODUCT, "channel": 2,
            "satellitePosition": satellite_position, "normalizationVersion": BAND2_NORMALIZATION_VERSION,
            "gapThreshold": BAND2_GAP_THRESHOLD, "minimumSunElevationDeg": BAND2_MIN_SUN_ELEVATION_DEG,
            "spatialBoxPixels": BAND2_BOX_PIXELS, "minimumValidPixels": BAND2_MIN_VALID_PIXELS,
            "temporalStatistic": "75th_percentile", "temporalWindowRequestedMinutesRelativeToRadar": [-BAND2_LOOKBACK_MINUTES, None],
            "temporalFramesUsed": len(values), "causalGapFraction": temporal_value,
            "maxGapFraction": temporal_value, "corroboratingFrames": sum(value >= BAND2_GAP_THRESHOLD for value in values),
            "maximumFrameGapFraction": max(values) if values else None,
            "instantaneousMaxGapFraction": closest.get("maxGapFraction"),
            "observedAt": closest.get("observedAt"), "createdAt": closest.get("createdAt"),
            "s3Key": closest.get("s3Key"), "sourceKeys": [frame.get("s3Key") for frame in frames],
            "frames": frames,
        }

    def close(self) -> None:
        for stored in self.datasets.values():
            frames = stored if isinstance(stored, list) else [stored]
            for _, dataset in frames:
                dataset.close()


def v1_sunlight_category(record: dict) -> str:
    features = record.get("features") or {}
    model, satellite = features.get("model") or {}, features.get("satellite") or {}
    dni, decision = model.get("directNormalIrradianceWm2"), satellite.get("decision")
    if dni is not None and float(dni) >= 200 and decision == "go":
        return "sunlit"
    if (dni is not None and float(dni) >= 120) or decision in ("go", "watch"):
        return "uncertain"
    if dni is None and decision is None:
        return "unknown"
    return "dark"


def combine_shadow(band2: dict, acmc: dict, metar: dict, dsrf: dict) -> tuple[float | None, dict]:
    components = {}
    if band2.get("maxGapFraction") is not None: components["band2"] = (float(band2["maxGapFraction"]), 0.45)
    if acmc.get("supportScore") is not None: components["acmc"] = (float(acmc["supportScore"]), 0.25)
    if metar.get("supportScore") is not None: components["metar"] = (float(metar["supportScore"]), 0.20)
    if dsrf.get("clearnessRatio") is not None: components["dsrf"] = (min(1.0, float(dsrf["clearnessRatio"])), 0.10)
    denominator = sum(weight for _, weight in components.values())
    probability = None if not denominator else sum(value * weight for value, weight in components.values()) / denominator
    return probability, {name: {"value": value, "weight": weight} for name, (value, weight) in components.items()}


def sunlight_state(band2: dict, acmc: dict, metar: dict, dsrf: dict) -> tuple[str, list[str]]:
    """Four-state research assessment; only overcast_supported is affirmative dark evidence."""
    frames = int(band2.get("temporalFramesUsed") or 0)
    corroborating = int(band2.get("corroboratingFrames") or 0)
    temporal_gap = band2.get("causalGapFraction")
    maximum_gap = band2.get("maximumFrameGapFraction")
    metar_score = metar.get("supportScore")
    acmc_score = acmc.get("supportScore")
    dsrf_ratio = dsrf.get("clearnessRatio")
    reasons = []
    if corroborating >= 2:
        return "sunlit_supported", ["band2_gap_in_multiple_causal_frames"]
    if corroborating >= 1 and metar_score is not None and float(metar_score) >= 0.75:
        return "sunlit_supported", ["band2_gap_plus_metar_few_or_scattered"]
    if corroborating >= 1 or (temporal_gap is not None and float(temporal_gap) >= BAND2_GAP_THRESHOLD):
        reasons.append("band2_gap_in_one_or_weakly_correlated_frame")
    if metar_score is not None and float(metar_score) >= 0.75:
        reasons.append("metar_few_or_scattered_clouds")
    if dsrf_ratio is not None and float(dsrf_ratio) >= 0.5:
        reasons.append("quality_valid_dsrf_clearness")
    if reasons:
        return "sunlight_plausible", reasons
    metar_overcast = not metar.get("available") or (metar_score is not None and float(metar_score) <= 0.3)
    if (
        frames >= 2 and maximum_gap is not None and float(maximum_gap) <= 0.1
        and acmc_score is not None and float(acmc_score) <= 0.1 and metar_overcast
    ):
        return "overcast_supported", ["causal_band2_window_and_regional_cloud_evidence_agree"]
    return "unresolved", ["missing_conflicting_or_insufficient_causal_evidence"]


def cleanup_shadow_cache(cache_dir: Path) -> None:
    """Close cached NetCDF datasets and remove per-run downloads from shadow /tmp."""
    root = cache_dir.resolve()
    for key, dataset in list(_DATASET_CACHE.items()):
        try:
            path = Path(key).resolve()
            if root == path or root in path.parents:
                dataset.close()
                _DATASET_CACHE.pop(key, None)
        except Exception:
            continue
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def enrich_sunlight_v2(
    records: list[dict],
    candidates: list[dict],
    radar_observed_at: str,
    cache_dir: Path,
    session: requests.Session | None = None,
    assessment_processing_at: str | None = None,
    metars: list[dict] | None = None,
    metar_method_version: str = METAR_METHOD_VERSION,
) -> dict:
    if not records or not candidates:
        return {"ok": True, "skipped": True, "reason": "no candidate decision records", "methodVersion": METHOD_VERSION, "records": len(records), "enriched": 0, "errors": [], "v1V2DisagreementRateByDisposition": {}}
    session = session or requests.Session()
    assessment_processing_at = assessment_processing_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    latency_seconds = round((parse_utc(assessment_processing_at) - parse_utc(radar_observed_at)).total_seconds(), 3)
    record_by_id = {record.get("candidateId"): record for record in records}
    if metars is None:
        try:
            metars = fetch_metars(session)
            metar_error = None
        except Exception as error:
            metars, metar_error = [], str(error)[:300]
    else:
        metars = list(metars)
        metar_error = None
    band2 = Band2Sampler(radar_observed_at, cache_dir / "band2", session=session, available_by=assessment_processing_at)
    errors = []
    enriched = 0
    disagreements: dict[str, list[bool]] = {}
    try:
        for candidate in candidates:
            record = record_by_id.get(candidate_id(candidate, radar_observed_at))
            if not record:
                continue
            try:
                dsrf_raw = sample(candidate["lat"], candidate["lon"], DEFAULT_PRODUCT, "auto", 3, cache_dir / "goes", observed_at=radar_observed_at, available_by=assessment_processing_at)
                acmc_raw = sample(candidate["lat"], candidate["lon"], FALLBACK_PRODUCT, "auto", 3, cache_dir / "goes", observed_at=radar_observed_at, available_by=assessment_processing_at)
                dsrf = dsrf_shadow(dsrf_raw, candidate["sunElevationDeg"])
                acmc = acmc_neighborhood(acmc_raw)
                band2_feature = band2.sample_candidate(candidate)
                metar = nearest_metars(candidate, metars, radar_observed_at, available_by=assessment_processing_at, method_version=metar_method_version) if metars else {"available": False, "error": metar_error, "methodVersion": metar_method_version, "availableBy": assessment_processing_at, "stations": [], "supportScore": None}
                probability, components = combine_shadow(band2_feature, acmc, metar, dsrf)
                state, state_reasons = sunlight_state(band2_feature, acmc, metar, dsrf)
                category = {"sunlit_supported": "sunlit", "sunlight_plausible": "uncertain", "unresolved": "unknown", "overcast_supported": "dark"}[state]
                v1_category = v1_sunlight_category(record)
                disagreement = None if "unknown" in (category, v1_category) else category != v1_category
                record["features"]["sunlightV2"] = {
                    "methodVersion": METHOD_VERSION, "operational": False,
                    "assessmentProcessingAt": assessment_processing_at, "radarObservedAt": radar_observed_at,
                    "enrichmentLatencySeconds": latency_seconds,
                    "directSunProbability": probability, "probabilityStatus": "provisional-uncalibrated", "shadowCategory": category,
                    "sunlightState": state, "sunlightStateReasons": state_reasons,
                    "v1SunlightCategory": v1_category, "disagreesWithV1": disagreement,
                    "componentsUsed": components, "band2": band2_feature, "acmc": acmc,
                    "metar": metar, "dsrf": dsrf, "cameraIllumination": None,
                }
                if disagreement is not None:
                    disagreements.setdefault(record.get("disposition", "unknown"), []).append(disagreement)
                enriched += 1
            except Exception as error:
                record["features"]["sunlightV2"] = {"methodVersion": METHOD_VERSION, "operational": False, "available": False, "error": str(error)[:300]}
                errors.append({"candidateId": record.get("candidateId"), "error": str(error)[:300]})
    finally:
        band2.close()
        cleanup_shadow_cache(cache_dir)
    rates = {name: {"records": len(values), "disagreements": sum(values), "rate": sum(values) / len(values)} for name, values in disagreements.items()}
    return {"ok": True, "methodVersion": METHOD_VERSION, "assessmentProcessingAt": assessment_processing_at, "enrichmentLatencySeconds": latency_seconds, "records": len(records), "enriched": enriched, "errors": errors, "v1V2DisagreementRateByDisposition": rates}


def safe_enrich_sunlight_v2(*args, **kwargs) -> dict:
    try:
        return enrich_sunlight_v2(*args, **kwargs)
    except Exception as error:
        print(f"[sunlight-v2] shadow enrichment failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300], "operationalImpact": False}
