"""NOAA MRMS discovery and decoding for the Rainbow Connector worker."""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

BUCKET = "noaa-mrms-pds"
REGION = "CONUS"
PRODUCT = "PrecipRate_00.00"
CADENCE_MINUTES = 2
MAX_AGE_MINUTES = 6
USER_AGENT = "RainbowConnectorMRMS/0.1"
_KEY_TIME = re.compile(r"_(\d{8})-(\d{6})\.grib2\.gz$")


@dataclass(frozen=True)
class MrmsObject:
    key: str
    observed_at: datetime
    url: str

    def age_minutes(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.observed_at).total_seconds() / 60


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def key_for_time(value: datetime, region: str = REGION, product: str = PRODUCT) -> str:
    value = ensure_utc(value)
    stamp = value.strftime("%Y%m%d-%H%M00")
    day = value.strftime("%Y%m%d")
    return f"{region}/{product}/{day}/MRMS_{product}_{stamp}.grib2.gz"


def object_from_key(key: str) -> MrmsObject:
    match = _KEY_TIME.search(key)
    if not match:
        raise ValueError(f"Unrecognized MRMS key: {key}")
    observed = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return MrmsObject(key=key, observed_at=observed, url=f"https://{BUCKET}.s3.amazonaws.com/{key}")


def candidate_keys(now: datetime | None = None, lookback_minutes: int = 30) -> Iterable[str]:
    """Yield likely keys newest-first, allowing MRMS several minutes to publish."""
    now = ensure_utc(now or datetime.now(timezone.utc)) - timedelta(minutes=2)
    rounded = now.replace(second=0, microsecond=0, minute=now.minute - now.minute % CADENCE_MINUTES)
    for minutes_back in range(0, lookback_minutes + 1, CADENCE_MINUTES):
        yield key_for_time(rounded - timedelta(minutes=minutes_back))


def discover_latest(
    now: datetime | None = None,
    session: requests.Session | None = None,
    lookback_minutes: int = 30,
) -> MrmsObject:
    session = session or requests.Session()
    for key in candidate_keys(now, lookback_minutes):
        hit = object_from_key(key)
        response = session.head(hit.url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if response.status_code == 200:
            return hit
        if response.status_code not in (403, 404):
            response.raise_for_status()
    raise RuntimeError(f"No MRMS {PRODUCT} object found in the last {lookback_minutes} minutes")


def download_and_expand(obj: MrmsObject, cache_dir: Path, session: requests.Session | None = None) -> Path:
    session = session or requests.Session()
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / Path(obj.key).name.removesuffix(".gz")
    if output.exists() and output.stat().st_size > 0:
        return output
    response = session.get(obj.url, headers={"User-Agent": USER_AGENT}, timeout=60)
    response.raise_for_status()
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(gzip.decompress(response.content))
    temporary.replace(output)
    return output


def open_precip_grid(path: Path):
    """Open a downloaded GRIB2 grid. Kept in the worker image, not the Vercel API."""
    try:
        import xarray as xr
        dataset = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    except (ImportError, ValueError) as exc:
        raise RuntimeError(
            "MRMS decoding requires the dedicated worker dependencies: cfgrib and eccodes"
        ) from exc
    variables = [name for name in dataset.data_vars if not name.startswith("valid_time")]
    if not variables:
        raise RuntimeError(f"MRMS file has no data variable: {path}")
    field = dataset[variables[0]].squeeze(drop=True)
    return field


def source_metadata(obj: MrmsObject, now: datetime | None = None) -> dict:
    age = obj.age_minutes(now)
    return {
        "provider": "NOAA MRMS",
        "product": PRODUCT,
        "observedAt": obj.observed_at.isoformat().replace("+00:00", "Z"),
        "ageMinutes": round(age, 1),
        "maxAgeMinutes": MAX_AGE_MINUTES,
        "fresh": -2 <= age <= MAX_AGE_MINUTES,
        "required": True,
        "s3Key": obj.key,
    }
