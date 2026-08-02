#!/usr/bin/env python
"""
Sample a GOES ABI Level 2 CONUS product at a latitude/longitude.

This is a prototype for The Rainbow Connector. It deliberately has no dependency
on the live site code. It downloads the latest matching public NOAA GOES object
from S3 over HTTPS, opens the NetCDF file, maps lat/lon to ABI fixed-grid scan
angles, and returns a compact JSON sunlight decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import requests
import xarray as xr


AWS_BUCKETS = {
    "east": "noaa-goes19",
    "west": "noaa-goes18",
}

DEFAULT_PRODUCT = "ABI-L2-DSRF"
FALLBACK_PRODUCT = "ABI-L2-ACMC"
SATELLITE_SPLIT_LON = -105.0
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
USER_AGENT = "RainbowGOESPrototype/0.1"
_DATASET_CACHE: dict[str, xr.Dataset] = {}
_LATEST_KEY_CACHE: dict[tuple[str, str, int], tuple[float, str]] = {}
_TARGET_KEY_CACHE: dict[tuple[str, str, str, str], str] = {}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sample GOES satellite sunlight/cloud product at a point.")
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--product", default=DEFAULT_PRODUCT, choices=[DEFAULT_PRODUCT, FALLBACK_PRODUCT])
    p.add_argument("--satellite", choices=["auto", "east", "west"], default="auto")
    p.add_argument("--hours-back", type=int, default=6)
    p.add_argument("--cache-dir", default=str(CACHE_DIR))
    p.add_argument("--keep", action="store_true", help="Keep downloaded files. Files are cached by default.")
    p.add_argument("--debug", action="store_true")
    return p.parse_args()


def satellite_for(lon: float, requested: str) -> str:
    if requested != "auto":
        return requested
    return "west" if lon < SATELLITE_SPLIT_LON else "east"


def list_s3_prefix(bucket: str, prefix: str) -> list[str]:
    url = f"https://{bucket}.s3.amazonaws.com/"
    r = requests.get(
        url,
        params={"list-type": "2", "prefix": prefix},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    r.raise_for_status()
    return re.findall(r"<Key>(.*?)</Key>", r.text)


def candidate_prefixes(product: str, hours_back: int, now: dt.datetime | None = None) -> list[str]:
    now = now or utc_now()
    prefixes = []
    for h in range(hours_back + 1):
        t = now - dt.timedelta(hours=h)
        doy = t.timetuple().tm_yday
        prefixes.append(f"{product}/{t.year}/{doy:03d}/{t.hour:02d}/")
    return prefixes


def latest_key(bucket: str, product: str, hours_back: int) -> str:
    cache_key = (bucket, product, hours_back)
    cached = _LATEST_KEY_CACHE.get(cache_key)
    # Reuse one listing across candidates in the same invocation, but force a
    # refresh between five-minute Lambda runs so a warm container cannot hold
    # a GOES key past the source-freshness ceiling.
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    matches: list[str] = []
    for prefix in candidate_prefixes(product, hours_back):
        keys = list_s3_prefix(bucket, prefix)
        matches.extend(k for k in keys if k.endswith(".nc"))
        if matches:
            break
    if not matches:
        raise RuntimeError(f"No {product} NetCDF files found in {bucket} over the last {hours_back} hours")
    latest = sorted(matches)[-1]
    _LATEST_KEY_CACHE[cache_key] = (time.monotonic(), latest)
    return latest


def key_near_time(
    bucket: str,
    product: str,
    target: dt.datetime,
    tolerance_minutes: int = 45,
    available_by: dt.datetime | None = None,
) -> str:
    target = target.astimezone(dt.UTC) if target.tzinfo else target.replace(tzinfo=dt.UTC)
    if available_by is not None:
        available_by = available_by.astimezone(dt.UTC) if available_by.tzinfo else available_by.replace(tzinfo=dt.UTC)
    cache_key = (
        bucket, product, target.strftime("%Y%m%dT%H%M"),
        available_by.strftime("%Y%m%dT%H%M%S") if available_by else "unbounded",
    )
    if cache_key in _TARGET_KEY_CACHE:
        return _TARGET_KEY_CACHE[cache_key]
    matches = []
    for hour_delta in (-1, 0, 1):
        moment = target + dt.timedelta(hours=hour_delta)
        prefix = f"{product}/{moment.year}/{moment.timetuple().tm_yday:03d}/{moment.hour:02d}/"
        for key in list_s3_prefix(bucket, prefix):
            if not key.endswith(".nc"):
                continue
            stamp = scan_start_from_key(key)
            if not stamp:
                continue
            observed = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            created = object_creation_from_key(key)
            if available_by is not None and (created is None or parse_timestamp(created) > available_by):
                continue
            delta = abs((observed - target).total_seconds()) / 60
            if delta <= tolerance_minutes:
                matches.append((delta, key))
    if not matches:
        raise RuntimeError(f"No {product} frame found within {tolerance_minutes} minutes of {target.isoformat()}")
    selected = min(matches)[1]
    _TARGET_KEY_CACHE[cache_key] = selected
    return selected


def download_key(bucket: str, key: str, cache_dir: Path, session: requests.Session | None = None) -> Path:
    session = session or requests
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / key.replace("/", "_")
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60, stream=True) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        tmp.replace(dest)
    return dest


def parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(dt.UTC) if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def timestamp_from_key(key: str, marker: str) -> str | None:
    m = re.search(rf"_{re.escape(marker)}(\d{{4}})(\d{{3}})(\d{{2}})(\d{{2}})(\d{{2}})", key)
    if not m:
        return None
    year, doy, hour, minute, second = map(int, m.groups())
    t = dt.datetime(year, 1, 1, tzinfo=dt.UTC) + dt.timedelta(
        days=doy - 1, hours=hour, minutes=minute, seconds=second
    )
    return t.isoformat().replace("+00:00", "Z")


def scan_start_from_key(key: str) -> str | None:
    return timestamp_from_key(key, "s")


def object_creation_from_key(key: str) -> str | None:
    """Return the GOES object creation time encoded by the filename's c field."""
    return timestamp_from_key(key, "c")


def scan_age_minutes(observed_at: str | None, reference_at: str | dt.datetime | None = None) -> int | None:
    if not observed_at:
        return None
    t = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if reference_at is None:
        reference = utc_now()
    elif isinstance(reference_at, str):
        reference = parse_timestamp(reference_at)
    else:
        reference = reference_at.astimezone(dt.UTC) if reference_at.tzinfo else reference_at.replace(tzinfo=dt.UTC)
    return int(round((reference - t).total_seconds() / 60))


def variable_name(ds: xr.Dataset, product: str) -> str:
    if product == DEFAULT_PRODUCT:
        for name in ("DSR", "Rad", "Sectorized_CMI"):
            if name in ds:
                return name
    if product == FALLBACK_PRODUCT:
        for name in ("BCM", "ACM", "Mask"):
            if name in ds:
                return name
    data_vars = [name for name in ds.data_vars if not name.startswith("DQF")]
    if not data_vars:
        raise RuntimeError("No sampleable data variable found in NetCDF file")
    return data_vars[0]


def projection_attrs(ds: xr.Dataset) -> dict[str, Any]:
    if "goes_imager_projection" not in ds:
        raise RuntimeError("Dataset has no goes_imager_projection variable")
    return dict(ds["goes_imager_projection"].attrs)


def latlon_to_scan_xy(lat: float, lon: float, attrs: dict[str, Any]) -> tuple[float, float]:
    """Convert geodetic lat/lon degrees to GOES fixed-grid scan angles.

    Formula follows the GOES-R fixed grid geometry. It returns scan angle x/y in
    radians, which are then matched to the x/y coordinate arrays in the file.
    """
    r_eq = float(attrs.get("semi_major_axis", 6378137.0))
    r_pol = float(attrs.get("semi_minor_axis", 6356752.31414))
    h = float(attrs["perspective_point_height"]) + r_eq
    lon0 = math.radians(float(attrs["longitude_of_projection_origin"]))

    phi = math.radians(lat)
    lam = math.radians(lon) - lon0
    e2 = (r_eq * r_eq - r_pol * r_pol) / (r_eq * r_eq)

    phi_c = math.atan((r_pol * r_pol) / (r_eq * r_eq) * math.tan(phi))
    r_c = r_pol / math.sqrt(1 - e2 * math.cos(phi_c) ** 2)

    sx = h - r_c * math.cos(phi_c) * math.cos(lam)
    sy = -r_c * math.cos(phi_c) * math.sin(lam)
    sz = r_c * math.sin(phi_c)

    visible = h * (h - sx) >= sy * sy + (r_eq * r_eq / (r_pol * r_pol)) * sz * sz
    if not visible:
        raise RuntimeError("Point is outside satellite view")

    x = math.asin(-sy / math.sqrt(sx * sx + sy * sy + sz * sz))
    y = math.atan(sz / sx)
    return x, y


def nearest_index(values: np.ndarray, target: float) -> int:
    return int(np.nanargmin(np.abs(values - target)))


def decode_value(raw: Any) -> float | int | None:
    try:
        value = raw.item() if hasattr(raw, "item") else raw
    except Exception:
        value = raw
    if value is None:
        return None
    try:
        f = float(value)
    except Exception:
        return value
    if not math.isfinite(f):
        return None
    if abs(f - round(f)) < 1e-9:
        return int(round(f))
    return f


def numeric_bounds(ds: xr.Dataset, name: str) -> list[float] | None:
    if name not in ds:
        return None
    values = np.asarray(ds[name].values).reshape(-1)
    decoded = [decode_value(value) for value in values]
    if len(decoded) != 2 or any(value is None for value in decoded):
        return None
    return [float(decoded[0]), float(decoded[1])]


def dsrf_quality_metadata(ds: xr.Dataset, ix: int, iy: int) -> dict[str, Any]:
    dqf = None
    if "DQF" in ds:
        dqf = decode_value(ds["DQF"].isel(x=ix, y=iy).values)
    return {
        "dqf": dqf,
        "dqfMeaning": "good_quality" if dqf == 0 else "degraded_or_invalid" if dqf == 1 else "unknown",
        "quantitativeSolarZenithBoundsDeg": numeric_bounds(ds, "quantitative_solar_zenith_angle_bounds"),
        "retrievalSolarZenithBoundsDeg": numeric_bounds(ds, "retrieval_solar_zenith_angle_bounds"),
    }


def decision(product: str, value: float | int | None) -> tuple[bool | None, str]:
    if value is None:
        return None, "unknown"
    if product == DEFAULT_PRODUCT:
        v = float(value)
        if v >= 250:
            return True, "strong"
        if v >= 120:
            return True, "moderate"
        if v >= 50:
            return True, "weak"
        return False, "blocked"
    # ABI-L2-ACMC BCM flag meanings:
    # 0 = clear_or_probably_clear, 1 = cloudy_or_probably_cloudy.
    v = int(value)
    if v == 0:
        return True, "clear-mask"
    if v == 1:
        return False, "cloudy-mask"
    return None, "unknown-mask"


def sample(
    lat: float,
    lon: float,
    product: str,
    satellite: str,
    hours_back: int,
    cache_dir: Path,
    observed_at: str | dt.datetime | None = None,
    available_by: str | dt.datetime | None = None,
) -> dict[str, Any]:
    sat = satellite_for(lon, satellite)
    bucket = AWS_BUCKETS[sat]
    if observed_at is not None:
        target = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00")) if isinstance(observed_at, str) else observed_at
        cutoff = dt.datetime.fromisoformat(available_by.replace("Z", "+00:00")) if isinstance(available_by, str) else available_by
        key = key_near_time(bucket, product, target, available_by=cutoff)
    else:
        key = latest_key(bucket, product, hours_back)
    path = download_key(bucket, key, cache_dir)

    ds_key = str(path)
    ds = _DATASET_CACHE.get(ds_key)
    if ds is None:
      ds = xr.open_dataset(path, mask_and_scale=True)
      _DATASET_CACHE[ds_key] = ds
    try:
      var_name = variable_name(ds, product)
      attrs = projection_attrs(ds)
      x, y = latlon_to_scan_xy(lat, lon, attrs)
      xs = ds["x"].values
      ys = ds["y"].values
      ix = nearest_index(xs, x)
      iy = nearest_index(ys, y)
      arr = ds[var_name]
      raw = arr.isel(x=ix, y=iy).values
      value = decode_value(raw)
      units = arr.attrs.get("units")
      sunlit, confidence = decision(product, value)
      observed_at = scan_start_from_key(key)
      created_at = object_creation_from_key(key)
      cutoff_text = available_by.isoformat().replace("+00:00", "Z") if isinstance(available_by, dt.datetime) else available_by
      causal_eligible = None if available_by is None else bool(
          created_at and parse_timestamp(created_at) <= parse_timestamp(str(cutoff_text))
      )
      quality = dsrf_quality_metadata(ds, ix, iy) if product == DEFAULT_PRODUCT else {}
      return {
          "lat": lat,
          "lon": lon,
          "satellite": "GOES-19" if sat == "east" else "GOES-18",
          "satellitePosition": sat,
          "bucket": bucket,
          "product": product,
          "variable": var_name,
          "observedAt": observed_at,
          "createdAt": created_at,
          "causalAvailableBy": cutoff_text,
          "causalEligible": causal_eligible,
          "observedAgeMinutes": scan_age_minutes(observed_at, available_by),
          "s3Key": key,
          "cachePath": str(path),
          "scanAngle": {"x": x, "y": y},
          "pixel": {"x": ix, "y": iy},
          "value": value,
          "units": units,
          "sunlit": sunlit,
          "confidence": confidence,
          **quality,
      }
    except Exception:
      raise


def main() -> int:
    args = parse_args()
    try:
        out = sample(
            lat=args.lat,
            lon=args.lon,
            product=args.product,
            satellite=args.satellite,
            hours_back=args.hours_back,
            cache_dir=Path(args.cache_dir),
        )
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    except Exception as err:
        if args.product != FALLBACK_PRODUCT:
            try:
                out = sample(
                    lat=args.lat,
                    lon=args.lon,
                    product=FALLBACK_PRODUCT,
                    satellite=args.satellite,
                    hours_back=args.hours_back,
                    cache_dir=Path(args.cache_dir),
                )
                out["primaryError"] = str(err)
                print(json.dumps(out, indent=2, sort_keys=True))
                return 0
            except Exception as fallback_err:
                print(json.dumps({"ok": False, "error": str(err), "fallbackError": str(fallback_err)}, indent=2), file=sys.stderr)
                return 1
        print(json.dumps({"ok": False, "error": str(err)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
