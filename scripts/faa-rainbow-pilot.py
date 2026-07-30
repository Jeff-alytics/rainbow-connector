#!/usr/bin/env python3
"""Build a one-time targeted pilot from FAA WeatherCam's rolling history."""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import torch
from PIL import Image
from transformers import AutoProcessor, CLIPModel


ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "faa"
IMAGES = OUT / "images"
REVIEW = OUT / "review"
SITES_CACHE = OUT / "sites.json"
ASOS_CACHE = OUT / "asos.csv"
BASE = "https://weathercams.faa.gov/api"
ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
IEM_NETWORK = "https://mesonet.agron.iastate.edu/geojson/network.php"
HEADERS = {"Referer": "https://weathercams.faa.gov/", "Origin": "https://weathercams.faa.gov", "User-Agent": "Mozilla/5.0 rainbow-connector-validation"}
WARM_STATES = {"AL","AR","AZ","CA","CO","FL","GA","KS","KY","LA","MD","MO","MS","NC","NM","NV","OK","SC","TN","TX","UT","VA","WV"}
PROMPTS = ("a photograph of a natural atmospheric rainbow in the sky", "a rainy sky without a rainbow",
           "a cloudy landscape without a rainbow", "sun glare or lens flare", "a low quality or obscured webcam image")
MODEL = "openai/clip-vit-base-patch32"


def solar(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    rad = math.pi / 180
    days = when.timestamp() / 86400 - .5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + .98560028 * days)
    longitude = anomaly + rad * (1.9148 * math.sin(anomaly) + .02 * math.sin(2 * anomaly) + .0003 * math.sin(3 * anomaly)) + rad * 102.9372 + math.pi
    declination = math.asin(math.sin(rad * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(math.sin(longitude) * math.cos(rad * 23.4397), math.cos(longitude))
    hour_angle = rad * (280.16 + 360.9856235 * days) + lon * rad - right_ascension
    latitude = lat * rad
    elevation = math.degrees(math.asin(math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)))
    azimuth = (math.degrees(math.atan2(math.sin(hour_angle), math.cos(hour_angle) * math.sin(latitude) - math.tan(declination) * math.cos(latitude))) + 180) % 360
    return elevation, azimuth


def angle_difference(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def get_sites() -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    if not SITES_CACHE.exists():
        response = requests.get(f"{BASE}/sites", headers=HEADERS, timeout=120)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError(data.get("error"))
        SITES_CACHE.write_text(json.dumps(data["payload"], indent=2) + "\n", encoding="utf-8")
    return json.loads(SITES_CACHE.read_text(encoding="utf-8"))


def get_asos(sites: list[dict], start: datetime, end: datetime) -> list[dict]:
    if not ASOS_CACHE.exists():
        stations = sorted({site.get("weatherStation", site.get("siteIdentifier")) for site in sites
                           if re.fullmatch(r"[A-Z0-9]{3,4}", site.get("weatherStation", site.get("siteIdentifier")) or "")})
        chunks = []
        for offset in range(0, len(stations), 70):
            params = [("station", station) for station in stations[offset:offset + 70]]
            params += [("data", "p01i"), ("data", "wxcodes"), ("report_type", "3"),
                       ("sts", start.strftime("%Y-%m-%dT%H:%M:%SZ")), ("ets", end.strftime("%Y-%m-%dT%H:%M:%SZ")),
                       ("tz", "Etc/UTC"), ("format", "onlycomma"), ("missing", "empty")]
            response = requests.get(ASOS, params=params, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=300)
            response.raise_for_status()
            lines = response.text.splitlines()
            chunks.extend(lines[1:] if chunks else lines)
        ASOS_CACHE.write_text("\n".join(chunks) + "\n", encoding="utf-8")
    return list(csv.DictReader(io.StringIO(ASOS_CACHE.read_text(encoding="utf-8"))))


def target_events(sites: list[dict], rows: list[dict], limit: int = 100, max_per_site: int = 2) -> list[dict]:
    station_sites = defaultdict(list)
    for site in sites:
        station_sites[site.get("weatherStation", site.get("siteIdentifier"))].append(site)
    candidates = []
    for row in rows:
        weather = (row.get("wxcodes") or "").upper()
        try:
            rain = float(row.get("p01i") or 0)
        except ValueError:
            rain = 0
        if rain <= 0 and not re.search(r"(?:RA|DZ|SH|TS|VCSH)", weather):
            continue
        try:
            when = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        for site in station_sites.get(row["station"], []):
            elevation, sun_azimuth = solar(when, float(site["latitude"]), float(site["longitude"]))
            if not 2 <= elevation <= 30:
                continue
            anti = (sun_azimuth + 180) % 360
            cameras = [camera for camera in site.get("cameras", []) if camera.get("cameraBearing") is not None and not camera.get("cameraOutOfOrder")]
            if not cameras:
                continue
            camera = min(cameras, key=lambda item: angle_difference(float(item["cameraBearing"]), anti))
            difference = angle_difference(float(camera["cameraBearing"]), anti)
            if difference > 65:
                continue
            rain_score = min(1, math.sqrt(max(rain, .0001) / .08))
            sun_score = math.exp(-((elevation - 13) / 11) ** 2)
            convective = .35 if "TS" in weather or "SH" in weather else 0
            score = 2 * sun_score + rain_score + convective + max(0, 1 - difference / 65)
            candidates.append({"site": site, "camera": camera, "observedAt": when.isoformat().replace("+00:00", "Z"),
                "rainInches": rain, "weather": weather, "sunElevation": round(elevation, 2),
                "sunAzimuth": round(sun_azimuth, 2), "antiSolarAzimuth": round(anti, 2),
                "cameraDifference": round(difference, 2), "metadataScore": round(score, 4)})
    candidates.sort(key=lambda item: item["metadataScore"], reverse=True)
    chosen, per_site = [], defaultdict(int)
    for item in candidates:
        site_id = item["site"]["siteId"]
        if per_site[site_id] >= max_per_site:
            continue
        center = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
        if any(old["site"]["siteId"] == site_id and abs((center - datetime.fromisoformat(old["observedAt"].replace("Z", "+00:00"))).total_seconds()) < 3 * 3600 for old in chosen):
            continue
        chosen.append(item); per_site[site_id] += 1
        if len(chosen) >= limit:
            break
    return chosen


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def attach_nearest_stations(sites: list[dict], max_distance_km: float = 60) -> list[dict]:
    cache = OUT / "iem-asos-stations.json"
    if cache.exists():
        stations = json.loads(cache.read_text(encoding="utf-8"))
    else:
        stations = []
        for state in sorted({site.get("state") for site in sites if site.get("state")}):
            response = requests.get(IEM_NETWORK, params={"network": f"{state}_ASOS"},
                                    headers={"User-Agent": HEADERS["User-Agent"]}, timeout=90)
            response.raise_for_status()
            for feature in response.json().get("features", []):
                coordinates = feature.get("geometry", {}).get("coordinates") or []
                sid = feature.get("properties", {}).get("sid")
                if sid and len(coordinates) >= 2:
                    stations.append({"station": sid, "state": state,
                                     "longitude": float(coordinates[0]), "latitude": float(coordinates[1])})
        cache.write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    matched = []
    for site in sites:
        latitude, longitude = float(site["latitude"]), float(site["longitude"])
        local = [station for station in stations if station["state"] == site.get("state")]
        if not local:
            continue
        nearest = min(local, key=lambda station: haversine_km(latitude, longitude, station["latitude"], station["longitude"]))
        distance = haversine_km(latitude, longitude, nearest["latitude"], nearest["longitude"])
        if distance <= max_distance_km:
            matched.append({**site, "weatherStation": nearest["station"],
                            "weatherStationDistanceKm": round(distance, 2)})
    print(f"Nearest-station coverage: {len(matched)}/{len(sites)} sites within {max_distance_km:g} km", flush=True)
    return matched


def image_sequence(item: dict) -> list[dict]:
    center = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
    start, end = center - timedelta(minutes=22), center + timedelta(minutes=22)
    url = f'{BASE}/sites/{item["site"]["siteId"]}/images'
    response = requests.get(url, params={"startTime": start.isoformat().replace("+00:00", "Z"),
        "endTime": end.isoformat().replace("+00:00", "Z")}, headers=HEADERS, timeout=90)
    response.raise_for_status()
    images = response.json().get("payload") or []
    camera_id = item["camera"]["cameraId"]
    matches = [image for image in images if image["cameraId"] == camera_id]
    matches.sort(key=lambda image: abs((datetime.fromisoformat(image["imageDatetime"].replace("Z", "+00:00")) - center).total_seconds()))
    selected = sorted(matches[:5], key=lambda image: image["imageDatetime"])
    frames = []
    IMAGES.mkdir(parents=True, exist_ok=True)
    for image in selected:
        name = image["imageFilename"]
        path = IMAGES / name
        if not path.exists():
            download = requests.get(image["imageUri"], headers=HEADERS, timeout=90)
            download.raise_for_status(); path.write_bytes(download.content)
        try:
            with Image.open(path) as source:
                source.verify()
        except Exception:
            continue
        frames.append({"path": path, "observedAt": image["imageDatetime"], "sourceUrl": image["imageUri"]})
    return frames


def normalize(value):
    tensor = value if isinstance(value, torch.Tensor) else value.pooler_output
    return tensor / tensor.norm(dim=-1, keepdim=True)


def score_frames(frames: list[dict]) -> None:
    processor = AutoProcessor.from_pretrained(MODEL)
    model = CLIPModel.from_pretrained(MODEL).eval()
    text_inputs = processor(text=list(PROMPTS), return_tensors="pt", padding=True)
    with torch.inference_mode():
        text = normalize(model.get_text_features(**text_inputs)).cpu()
    for start in range(0, len(frames), 16):
        batch = frames[start:start + 16]
        sources = [Image.open(frame["path"]).convert("RGB") for frame in batch]
        inputs = processor(images=sources, return_tensors="pt")
        with torch.inference_mode():
            similarities = normalize(model.get_image_features(**inputs)).cpu() @ text.T
        for frame, row, source in zip(batch, similarities, sources):
            array = np.asarray(source.resize((256, 256)).convert("HSV"), dtype=float) / 255
            frame["quality"] = round(float(array[..., 2].std() + array[..., 1].mean()), 4)
            frame["semanticScore"] = round(float(row[0] - row[1:].max()), 6)


def build_review(items: list[dict], review_dir: Path = REVIEW, batch: str = "01",
                 prefix: str = "faa", title: str = "FAA WeatherCam targeted rainbow pilot") -> Path:
    review_dir.mkdir(parents=True, exist_ok=True)
    articles, output = [], []
    for number, item in enumerate(items[:60], 1):
        candidate_id = f"{prefix}-{number:02d}"
        figures, result_frames = [], []
        for index, frame in enumerate(item["frames"], 1):
            filename = f"{candidate_id}-{index:02d}.jpg"
            destination = review_dir / filename
            if not destination.exists():
                destination.write_bytes(frame["path"].read_bytes())
            figures.append(f'<figure><a href="{filename}" target="_blank"><img src="{filename}"></a><figcaption>{html.escape(frame["observedAt"])}</figcaption></figure>')
            result_frames.append({"file": filename, "observedAt": frame["observedAt"], "sourceUrl": frame["sourceUrl"], "semanticScore": frame["semanticScore"]})
        site, camera = item["site"], item["camera"]
        facts = f'{candidate_id} · {site["siteName"]}, {site.get("state", "")} · camera {camera["cameraDirection"]} ({camera["cameraBearing"]}°) · {item["observedAt"]} · {item["weather"]} · sun {item["sunElevation"]:.1f}°'
        articles.append(f'<article><h2>{html.escape(facts)}</h2><div class="strip">{"".join(figures)}</div></article>')
        output.append({"candidateId": candidate_id, "siteId": site["siteId"], "station": site["siteIdentifier"], "siteName": site["siteName"],
            "state": site.get("state"), "latitude": site["latitude"], "longitude": site["longitude"], "cameraId": camera["cameraId"],
            "cameraDirection": camera["cameraDirection"], "cameraBearing": camera["cameraBearing"], "observedAt": item["observedAt"],
            "weather": item["weather"], "rainInches": item["rainInches"], "sunElevation": item["sunElevation"],
            "antiSolarAzimuth": item["antiSolarAzimuth"], "cameraDifference": item["cameraDifference"],
            "metadataScore": item["metadataScore"], "finalScore": item.get("finalScore"),
            "reviewTier": item.get("reviewTier", "ranked"), "weatherStation": site.get("weatherStation"),
            "weatherStationDistanceKm": site.get("weatherStationDistanceKm"), "frames": result_frames})
    page = review_dir / f"faa-review-batch-{batch}.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FAA held-out rainbow test</title>
<style>body{background:#101114;color:#eee;font:16px system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}article{background:#1b1d22;padding:16px;margin-bottom:30px}.strip{display:flex;gap:10px;overflow-x:auto}figure{flex:0 0 520px;margin:0}img{width:520px;height:340px;object-fit:contain;background:#000}figcaption{color:#bbb;padding:5px}h2{font-size:17px}</style></head><body><main><h1>FAA WeatherCam targeted rainbow pilot</h1>
<p>Forty ranked five-frame sequences from reported rain, low sun, and a camera facing the antisolar sector. Click any frame for full resolution.</p>'''+"".join(articles)+"</main></body></html>", encoding="utf-8")
    (review_dir / f"faa-review-batch-{batch}.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return page


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expansion", action="store_true", help="Run on sites held out from the original pilot")
    parser.add_argument("--network-expansion", action="store_true", help="Run the final nearest-station network test")
    args = parser.parse_args()
    global ASOS_CACHE, IMAGES
    now = datetime.now(timezone.utc)
    all_sites = get_sites()
    if args.network_expansion:
        pilot = json.loads((OUT / "faa-ranked-pilot.json").read_text(encoding="utf-8"))
        expansion = json.loads((OUT / "faa-ranked-expansion.json").read_text(encoding="utf-8"))
        held_out = {item["site"]["siteId"] for item in pilot + expansion}
        unused = [site for site in all_sites if site.get("country") == "US" and site.get("siteId") not in held_out
                  and site.get("siteActive") and site.get("cameras")]
        sites = attach_nearest_stations(unused)
        ASOS_CACHE = OUT / "asos-network-expansion.csv"
        IMAGES = OUT / "network-expansion-images"
    elif args.expansion:
        pilot = json.loads((OUT / "faa-ranked-pilot.json").read_text(encoding="utf-8"))
        held_out = {item["site"]["siteId"] for item in pilot}
        sites = [site for site in all_sites if site.get("country") == "US" and site.get("siteId") not in held_out
                 and site.get("siteActive") and site.get("cameras")]
        ASOS_CACHE = OUT / "asos-expansion.csv"
        IMAGES = OUT / "expansion-images"
    else:
        sites = [site for site in all_sites if site.get("country") == "US" and site.get("state") in WARM_STATES
                 and site.get("siteActive") and site.get("cameras")]
    rows = get_asos(sites, now - timedelta(days=27), now)
    events = target_events(sites, rows, limit=300 if args.network_expansion else 100,
                           max_per_site=1 if args.network_expansion else 2)
    usable, all_frames = [], []
    for number, event in enumerate(events, 1):
        event["frames"] = image_sequence(event)
        if len(event["frames"]) >= 3:
            usable.append(event); all_frames.extend(event["frames"])
        print(f"Fetched FAA event {number}/{len(events)}; {len(usable)} usable", flush=True)
    score_frames(all_frames)
    for event in usable:
        scores = sorted((frame["semanticScore"] for frame in event["frames"]), reverse=True)
        qualities = sorted((frame["quality"] for frame in event["frames"]), reverse=True)
        event["finalScore"] = scores[0] + .35 * scores[min(1, len(scores)-1)] + .04 * qualities[0] + .005 * event["metadataScore"]
    usable.sort(key=lambda event: event["finalScore"], reverse=True)
    if args.network_expansion:
        (OUT / "faa-ranked-network-expansion.json").write_text(json.dumps(usable, default=str, indent=2) + "\n", encoding="utf-8")
        high = [{**event, "reviewTier": "high"} for event in usable if event["finalScore"] >= .055][:20]
        near = [{**event, "reviewTier": "near-cutoff"} for event in usable if .035 <= event["finalScore"] < .055][:20]
        used = {(event["site"]["siteId"], event["observedAt"]) for event in high + near}
        weather_pool = [event for event in usable if (event["site"]["siteId"], event["observedAt"]) not in used
                        and 4 <= event["sunElevation"] <= 22 and event["cameraDifference"] <= 30]
        weather_pool.sort(key=lambda event: event["metadataScore"], reverse=True)
        weather = [{**event, "reviewTier": "weather-strong/image-weaker"} for event in weather_pool[:20]]
        review = high + near + weather
        print(f"Network review tiers: high={len(high)}, near={len(near)}, weather-strong={len(weather)}; usable={len(usable)}", flush=True)
        print(build_review(review, OUT / "network-expansion-review", "03", "faan", "FAA network-scale rainbow test"))
    elif args.expansion:
        (OUT / "faa-ranked-expansion.json").write_text(json.dumps(usable, default=str, indent=2) + "\n", encoding="utf-8")
        high_confidence = [event for event in usable if event["finalScore"] >= .055]
        print(f"Held-out high-confidence events: {len(high_confidence)}/{len(usable)}", flush=True)
        print(build_review(high_confidence, OUT / "expansion-review", "02", "faax", "FAA held-out rainbow test"))
    else:
        (OUT / "faa-ranked-pilot.json").write_text(json.dumps(usable, default=str, indent=2) + "\n", encoding="utf-8")
        print(build_review(usable))


if __name__ == "__main__":
    main()
