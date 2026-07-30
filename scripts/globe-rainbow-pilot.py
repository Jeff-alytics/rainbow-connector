#!/usr/bin/env python3
"""Build a targeted GLOBE Observer rainbow review pilot."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import torch
from PIL import Image
from transformers import AutoProcessor, CLIPModel


ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "globe"
CACHE = OUT / "api-cache"
IMAGES = OUT / "images"
REVIEW = OUT / "review"
API = "https://api.globe.gov/search/v1/measurement/protocol/measureddate/"
MODEL = "openai/clip-vit-base-patch32"
UA = {"User-Agent": "rainbow-connector-validation/1.0 (public GLOBE data research)"}
DIRECTIONS = ("North", "East", "South", "West", "Upward")
PROMPTS = (
    "a photograph of a natural atmospheric rainbow in the sky",
    "a rainy sky without a rainbow",
    "a cloudy sky without a rainbow",
    "sun glare or lens flare",
    "an indoor scene or object",
)


def solar_elevation(when_utc: datetime, lat: float, lon: float) -> float:
    rad = math.pi / 180
    days = when_utc.timestamp() / 86400 - .5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + .98560028 * days)
    longitude = anomaly + rad * (1.9148 * math.sin(anomaly) + .02 * math.sin(2 * anomaly) + .0003 * math.sin(3 * anomaly)) + rad * 102.9372 + math.pi
    declination = math.asin(math.sin(rad * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(math.sin(longitude) * math.cos(rad * 23.4397), math.cos(longitude))
    hour_angle = rad * (280.16 + 360.9856235 * days) + lon * rad - right_ascension
    latitude = lat * rad
    return math.degrees(math.asin(math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)))


def record_time(record: dict) -> tuple[datetime, float]:
    data = record["data"]
    local = datetime.fromisoformat(data["skyconditionsMeasuredAt"])
    # GLOBE's field is local civil time without an offset. Longitude gives a
    # stable approximation; a one-hour DST error is acceptable for this gate.
    offset = max(-12, min(14, round(float(record["longitude"]) / 15)))
    utc = (local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
    return local, solar_elevation(utc, float(record["latitude"]), float(record["longitude"]))


def photo_urls(record: dict) -> dict[str, str]:
    data = record["data"]
    return {direction: data.get(f"skyconditions{direction}PhotoUrl") for direction in DIRECTIONS}


def fetch_month(start: str, end: str) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"sky-{start}-{end}.json"
    if not path.exists():
        response = requests.get(API, params={"protocols": "sky_conditions", "startdate": start,
            "enddate": end, "geojson": "FALSE", "sample": "FALSE", "size": 20000}, headers=UA, timeout=300)
        response.raise_for_status()
        path.write_bytes(response.content)
    return json.loads(path.read_text(encoding="utf-8"))["results"]


def metadata_candidates(records: list[dict]) -> list[dict]:
    ranked = []
    for record in records:
        data = record["data"]
        urls = photo_urls(record)
        cover = (data.get("skyconditionsCloudCover") or "").lower()
        if not data.get("skyconditionsRainingSnowing") or cover not in {"isolated", "scattered", "broken"}:
            continue
        if not all(urls.values()):
            continue
        if data.get("skyconditionsHeavySnow") or data.get("skyconditionsBlowingSnow"):
            continue
        if data.get("skyconditionsSnowIce") and not data.get("skyconditionsHeavyRain"):
            continue
        try:
            local, elevation = record_time(record)
        except (KeyError, TypeError, ValueError):
            continue
        if not 2 <= elevation <= 32:
            continue
        cover_score = {"isolated": 1.3, "scattered": 1.1, "broken": .8}[cover]
        sun_score = math.exp(-((elevation - 13) / 11) ** 2)
        weather_score = .35 if data.get("skyconditionsHeavyRain") else 0
        weather_score += .2 if data.get("skyconditionsStandingWater") else 0
        cloud_score = .25 if data.get("skyconditionsCumulonimbus") or data.get("skyconditionsCumulus") else 0
        ranked.append({"record": record, "urls": urls, "local": local.isoformat(),
            "sunElevation": round(elevation, 2), "metadataScore": cover_score + sun_score + weather_score + cloud_score})
    ranked.sort(key=lambda item: item["metadataScore"], reverse=True)
    # Avoid near-duplicate submissions from the same site and hour.
    chosen, seen = [], set()
    for item in ranked:
        record = item["record"]
        key = (record.get("siteId"), item["local"][:13])
        if key in seen:
            continue
        seen.add(key)
        chosen.append(item)
        if len(chosen) >= 60:
            break
    return chosen


def download(items: list[dict]) -> list[dict]:
    IMAGES.mkdir(parents=True, exist_ok=True)
    frames = []
    for number, item in enumerate(items, 1):
        pid = item["record"]["pid"]
        item["frames"] = []
        for direction, url in item["urls"].items():
            path = IMAGES / f"{pid}-{direction.lower()}.jpg"
            if not path.exists():
                response = requests.get(url, headers=UA, timeout=90)
                response.raise_for_status()
                path.write_bytes(response.content)
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception:
                path.unlink(missing_ok=True)
                continue
            frame = {"path": path, "direction": direction, "url": url}
            item["frames"].append(frame)
            frames.append(frame)
        print(f"Downloaded observation {number}/{len(items)}", flush=True)
    return frames


def normalized(value):
    tensor = value if isinstance(value, torch.Tensor) else value.pooler_output
    return tensor / tensor.norm(dim=-1, keepdim=True)


def semantic_scores(frames: list[dict]) -> None:
    processor = AutoProcessor.from_pretrained(MODEL)
    model = CLIPModel.from_pretrained(MODEL).eval()
    text_inputs = processor(text=list(PROMPTS), return_tensors="pt", padding=True)
    with torch.inference_mode():
        text = normalized(model.get_text_features(**text_inputs)).cpu()
    for start in range(0, len(frames), 16):
        batch = frames[start:start + 16]
        images = [Image.open(frame["path"]).convert("RGB") for frame in batch]
        inputs = processor(images=images, return_tensors="pt")
        with torch.inference_mode():
            similarity = normalized(model.get_image_features(**inputs)).cpu() @ text.T
        for frame, row in zip(batch, similarity):
            frame["semanticScore"] = round(float(row[0] - row[1:].max()), 6)
            frame["rainbowSimilarity"] = round(float(row[0]), 6)


def build_review(items: list[dict]) -> Path:
    REVIEW.mkdir(parents=True, exist_ok=True)
    cards, results = [], []
    for number, item in enumerate(items[:60], 1):
        candidate_id = f"globe-{number:02d}"
        record = item["record"]
        figures = []
        result_frames = []
        for frame in item["frames"]:
            filename = f"{candidate_id}-{frame['direction'].lower()}.jpg"
            destination = REVIEW / filename
            if not destination.exists():
                destination.write_bytes(frame["path"].read_bytes())
            figures.append(f'<figure><a href="{filename}" target="_blank"><img src="{filename}" alt="{candidate_id} {frame["direction"]}"></a><figcaption>{frame["direction"]}</figcaption></figure>')
            result_frames.append({"direction": frame["direction"], "file": filename, "sourceUrl": frame["url"],
                "semanticScore": frame["semanticScore"]})
        facts = f"{candidate_id} · {record.get('countryName', '')} · {item['local']} · {record['data'].get('skyconditionsCloudCover')} · sun ≈ {item['sunElevation']:.1f}°"
        cards.append(f'<article><h2>{html.escape(facts)}</h2><div class="strip">{"".join(figures)}</div></article>')
        results.append({"candidateId": candidate_id, "pid": record["pid"], "latitude": record["latitude"],
            "longitude": record["longitude"], "country": record.get("countryName"), "observedAtLocal": item["local"],
            "cloudCover": record["data"].get("skyconditionsCloudCover"), "sunElevationApprox": item["sunElevation"],
            "modelPeak": item["modelPeak"], "frames": result_frames})
    page = REVIEW / "globe-review-batch-60.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>GLOBE rainbow pilot</title><style>body{background:#101114;color:#eee;font:16px system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}
article{background:#1b1d22;padding:16px;margin:0 0 30px;border-radius:8px}.strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:10px}
figure{flex:0 0 360px;margin:0;background:#08090a}img{display:block;width:360px;height:270px;object-fit:contain}figcaption{padding:6px 10px;color:#bbb}
h1{margin-top:0}h2{font-size:18px}p{color:#cfd2d8}</style></head><body><main><h1>GLOBE Observer · targeted rainbow pilot</h1>
<p>Sixty ranked observations with reported precipitation and mixed cloud cover. Each row is North, East, South, West, Upward. Click for the original-resolution review copy.</p>'''
        + "".join(cards) + "</main></body></html>", encoding="utf-8")
    (REVIEW / "globe-review-batch-60.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return page


def main() -> None:
    records = []
    for start, end in (("2026-07-01", "2026-07-25"), ("2026-06-01", "2026-06-30"),
                       ("2026-05-01", "2026-05-31"), ("2026-04-01", "2026-04-30"),
                       ("2025-10-01", "2025-10-31"), ("2025-09-01", "2025-09-30"),
                       ("2025-08-01", "2025-08-31"), ("2025-07-01", "2025-07-31"),
                       ("2025-06-01", "2025-06-30"), ("2025-05-01", "2025-05-31"),
                       ("2025-04-01", "2025-04-30")):
        records.extend(fetch_month(start, end))
    items = metadata_candidates(records)
    frames = download(items)
    semantic_scores(frames)
    usable = [item for item in items if len(item["frames"]) == 5]
    for item in usable:
        scores = sorted((frame["semanticScore"] for frame in item["frames"]), reverse=True)
        item["modelPeak"] = scores[0]
        item["modelSecond"] = scores[1]
        item["finalScore"] = scores[0] + .35 * scores[1] + .015 * item["metadataScore"]
    usable.sort(key=lambda item: item["finalScore"], reverse=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "globe-ranked-pilot.json").write_text(json.dumps([{k: v for k, v in item.items() if k != "record"} | {
        "pid": item["record"]["pid"], "country": item["record"].get("countryName")} for item in usable], default=str, indent=2) + "\n", encoding="utf-8")
    print(build_review(usable))


if __name__ == "__main__":
    main()
