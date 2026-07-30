#!/usr/bin/env python3
"""Rank NASA GSFC all-sky archive times for rainbow-friendly conditions.

This deliberately reads only the small station-data files.  Large all-sky
archives are downloaded later, and only for dates that survive this filter.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "gsfc"
CACHE = OUT / "weather-cache"
ASOS_CACHE = OUT / "bwi-asos.csv"
RESULTS = OUT / "gsfc-ranked-targets.json"
ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
GSFC = "https://har.gsfc.nasa.gov/storm/WEATHER/ASCII_WX_STATION_DATA_HISTORICAL"
LAT, LON = 38.991, -76.841
LOCAL = ZoneInfo("America/New_York")
UA = {"User-Agent": "rainbow-connector-validation/1.0 (research targeting)"}


def solar_elevation(when: datetime, lat: float = LAT, lon: float = LON) -> float:
    """Compact NOAA-style solar elevation approximation; `when` must be aware."""
    rad = math.pi / 180
    utc = when.astimezone(timezone.utc)
    days = utc.timestamp() / 86400 - .5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + .98560028 * days)
    longitude = (
        anomaly
        + rad * (1.9148 * math.sin(anomaly) + .02 * math.sin(2 * anomaly) + .0003 * math.sin(3 * anomaly))
        + rad * 102.9372
        + math.pi
    )
    declination = math.asin(math.sin(rad * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(math.sin(longitude) * math.cos(rad * 23.4397), math.cos(longitude))
    hour_angle = rad * (280.16 + 360.9856235 * days) + lon * rad - right_ascension
    latitude = lat * rad
    return math.degrees(math.asin(
        math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)
    ))


def get_bwi_rows(refresh: bool = False) -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    if refresh or not ASOS_CACHE.exists():
        params = {
            "station": "BWI", "data": ["p01i", "wxcodes"], "report_type": "3",
            "sts": "2023-04-17T00:00:00Z", "ets": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tz": "Etc/UTC", "format": "onlycomma", "missing": "empty",
        }
        response = requests.get(ASOS, params=params, headers=UA, timeout=300)
        response.raise_for_status()
        ASOS_CACHE.write_text(response.text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(ASOS_CACHE.read_text(encoding="utf-8"))))


def rain_events(rows: list[dict]) -> list[dict]:
    events = []
    for row in rows:
        weather = (row.get("wxcodes") or "").upper()
        try:
            amount = float(row.get("p01i") or 0)
        except ValueError:
            amount = 0
        if amount <= 0 and not re.search(r"(^|[+\- ])(?:RA|SHRA|DZ)", weather):
            continue
        when = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        elevation = solar_elevation(when)
        if 1.5 <= elevation <= 28:
            events.append({"time": when, "amount": amount, "weather": weather, "sunElevation": elevation})
    return events


def fetch_station_day(day: date) -> Path | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = day.strftime("%Y%m%d")
    path = CACHE / f"{stamp}_All_Data_Common_Time.dat"
    if path.exists() and path.stat().st_size > 1000:
        return path
    url = f"{GSFC}/{day.year}/{stamp}/{stamp}_All_Data_Common_Time.dat"
    response = requests.get(url, headers=UA, timeout=180)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    path.write_bytes(response.content)
    return path


def number(row: dict, key: str) -> float:
    try:
        value = float(row.get(key, "nan"))
        return value if math.isfinite(value) else 0.0
    except (TypeError, ValueError):
        return 0.0


def local_time(text: str) -> datetime:
    # The GSFC files are calendar days in local US Eastern civil time.
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL)


def score_day(path: Path, bwi_events: list[dict]) -> list[dict]:
    candidates = []
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                when = local_time(row["TIME"])
            except (KeyError, ValueError):
                continue
            elevation = solar_elevation(when)
            if not 1.5 <= elevation <= 28:
                continue
            irradiance = max(0, number(row, "SlrW"))
            clear_proxy = max(35, 950 * math.sin(math.radians(elevation)))
            sun_fraction = min(1.5, irradiance / clear_proxy)
            pluvio = max(0, number(row, "intensity_RT"))
            present = max(0, number(row, "CS125_Intensity"))
            tip = max(0, number(row, "Tipping_Rain_in"))
            local_wet = max(pluvio / 5, present / 5, tip * 20)
            nearest = min(
                bwi_events,
                key=lambda event: abs((event["time"] - when.astimezone(timezone.utc)).total_seconds()),
                default=None,
            )
            minutes = abs((nearest["time"] - when.astimezone(timezone.utc)).total_seconds()) / 60 if nearest else 9999
            nearby_rain = max(0, 1 - minutes / 90)
            # Require actual sunlight and either local precipitation or nearby BWI rain.
            if irradiance < 45 or sun_fraction < .22 or (local_wet <= 0 and nearby_rain <= 0):
                continue
            low_sun_shape = math.exp(-((elevation - 12) / 10) ** 2)
            score = 3.2 * min(1, sun_fraction) + 2.8 * min(1, local_wet) + 1.8 * nearby_rain + low_sun_shape
            candidates.append({
                "timeLocal": when.isoformat(),
                "timeUtc": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "sunElevation": round(elevation, 2),
                "irradianceWm2": round(irradiance, 2),
                "sunFractionProxy": round(sun_fraction, 3),
                "pluvioIntensity": round(pluvio, 4),
                "presentWeatherIntensity": round(present, 4),
                "minutesFromBwiRain": round(minutes, 1) if nearest else None,
                "bwiWeather": nearest["weather"] if nearest else "",
                "score": round(score, 4),
            })
    # Avoid returning dozens of adjacent minutes from one event.
    chosen = []
    for item in sorted(candidates, key=lambda candidate: candidate["score"], reverse=True):
        when = datetime.fromisoformat(item["timeLocal"])
        if any(abs((when - datetime.fromisoformat(old["timeLocal"])).total_seconds()) < 20 * 60 for old in chosen):
            continue
        chosen.append(item)
    return chosen[:5]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="number of best BWI-rain dates to inspect")
    parser.add_argument("--refresh-asos", action="store_true")
    args = parser.parse_args()

    events = rain_events(get_bwi_rows(args.refresh_asos))
    by_day: dict[date, list[dict]] = defaultdict(list)
    for event in events:
        by_day[event["time"].astimezone(LOCAL).date()].append(event)
    ranked_days = sorted(
        by_day,
        key=lambda day: max((1 + min(1, event["amount"] * 10)) * math.exp(-((event["sunElevation"] - 12) / 11) ** 2)
                            for event in by_day[day]),
        reverse=True,
    )[:args.days]

    targets = []
    for index, day in enumerate(ranked_days, 1):
        path = fetch_station_day(day)
        if path:
            for target in score_day(path, by_day[day]):
                target["date"] = day.isoformat()
                target["stationFile"] = str(path)
                targets.append(target)
        print(f"Inspected {index}/{len(ranked_days)} dates; {len(targets)} viable windows", flush=True)

    targets.sort(key=lambda target: target["score"], reverse=True)
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": "BWI rain + GSFC local precipitation/present-weather + measured solar irradiance + low sun",
        "bwiLowSunRainObservations": len(events),
        "datesInspected": len(ranked_days),
        "targets": targets,
    }
    RESULTS.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {RESULTS}")
    for target in targets[:20]:
        print(json.dumps(target, separators=(",", ":")))


if __name__ == "__main__":
    main()
