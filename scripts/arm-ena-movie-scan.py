#!/usr/bin/env python3
"""Semantically screen every compact ARM ENA all-sky movie."""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from transformers import AutoProcessor, CLIPModel


ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
ENA = ROOT / "validation" / "arm-ena"
MOVIES = ENA / "movies"
OUTPUT = ENA / "movie-semantic-ranked.json"
MODEL = "openai/clip-vit-base-patch32"
PROMPTS = ("a photograph of a natural atmospheric rainbow in the sky", "a cloudy sky without a rainbow",
           "sun glare or lens flare in a sky camera", "a colorful camera artifact")
LAT, LON = 39.0916, -28.0257
SIGN, OFFSET = -1, 254.97494434003312


def movie_start(path: Path) -> datetime:
    match = re.search(r"\.(20\d{6})\.(\d{6})\.mpg$", path.name)
    if not match:
        raise ValueError(path.name)
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def solar(when: datetime) -> tuple[float, float]:
    rad = math.pi / 180
    days = when.timestamp() / 86400 - .5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + .98560028 * days)
    longitude = anomaly + rad * (1.9148 * math.sin(anomaly) + .02 * math.sin(2 * anomaly) + .0003 * math.sin(3 * anomaly)) + rad * 102.9372 + math.pi
    declination = math.asin(math.sin(rad * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(math.sin(longitude) * math.cos(rad * 23.4397), math.cos(longitude))
    hour_angle = rad * (280.16 + 360.9856235 * days) + LON * rad - right_ascension
    latitude = LAT * rad
    elevation = math.degrees(math.asin(math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)))
    azimuth = (math.degrees(math.atan2(math.sin(hour_angle), math.cos(hour_angle) * math.sin(latitude) - math.tan(declination) * math.cos(latitude))) + 180) % 360
    return elevation, azimuth


def dewarp(image: Image.Image, anti_angle: float) -> Image.Image:
    image = image.convert("RGB")
    width, height = 640, 360
    cx, cy, fisheye_radius = image.width / 2, image.height / 2, min(image.size) * .46875
    azimuth = np.linspace(-55, 55, width)
    elevation = np.linspace(45, -3, height)
    az_grid, el_grid = np.meshgrid(azimuth, elevation)
    radius = fisheye_radius * (90 - el_grid) / 90
    angle = anti_angle + np.radians(az_grid)
    x = np.clip(np.rint(cx + radius * np.cos(angle)), 0, image.width - 1).astype(int)
    y = np.clip(np.rint(cy + radius * np.sin(angle)), 0, image.height - 1).astype(int)
    return Image.fromarray(np.asarray(image)[y, x])


def decode(path: Path) -> list[Image.Image]:
    # Every source frame represents 15 real seconds; take every twentieth
    # source frame for a stable five-real-minute sampling interval.
    result = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path),
        "-vf", "select=not(mod(n\\,20))", "-vsync", "vfr", "-q:v", "5",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, check=True)
    data, images, offset = result.stdout, [], 0
    while True:
        start = data.find(b"\xff\xd8", offset)
        if start < 0:
            break
        end = data.find(b"\xff\xd9", start + 2)
        if end < 0:
            break
        images.append(Image.open(io.BytesIO(data[start:end + 2])).convert("RGB"))
        offset = end + 2
    return images


def normalize(value):
    tensor = value if isinstance(value, torch.Tensor) else value.pooler_output
    return tensor / tensor.norm(dim=-1, keepdim=True)


def reviewed_times() -> list[datetime]:
    result = []
    for path in (ROOT / "validation" / "arm-lamont" / "human-labels.json",
                 ENA / "image-first-human-labels.json", ENA / "clip-human-labels.json"):
        if not path.exists():
            continue
        for item in json.loads(path.read_text(encoding="utf-8")).get("labels", []):
            if item.get("centerTime"):
                result.append(datetime.fromisoformat(item["centerTime"].replace("Z", "+00:00")))
    return result


def scan_movie(path: Path, processor, model, text_vectors, known: list[datetime]) -> list[dict]:
    start = movie_start(path)
    panels, metadata = [], []
    for index, image in enumerate(decode(path)):
        when = start + timedelta(seconds=index * 300)
        elevation, bearing = solar(when)
        if not 0 < elevation < 28 or any(abs((when - old).total_seconds()) <= 3600 for old in known):
            continue
        anti_angle = math.radians((SIGN * ((bearing + 180) % 360) + OFFSET) % 360)
        panel = dewarp(image, anti_angle)
        hsv = np.asarray(panel.resize((256, 144)).convert("HSV"), dtype=float) / 255
        quality = float(hsv[..., 2].std() + hsv[..., 1].mean())
        if quality < .09:
            continue
        panels.append(panel)
        metadata.append({"observedAt": when.isoformat().replace("+00:00", "Z"), "movie": path.name,
            "sampleIndex": index, "sunElevationDeg": round(elevation, 2), "quality": round(quality, 4)})
    records = []
    for offset in range(0, len(panels), 24):
        batch = panels[offset:offset + 24]
        inputs = processor(images=batch, return_tensors="pt")
        with torch.inference_mode():
            similarities = normalize(model.get_image_features(**inputs)).cpu() @ text_vectors.T
        for item, row in zip(metadata[offset:offset + 24], similarities):
            records.append({**item, "semanticScore": round(float(row[0] - row[1:].max()), 6),
                "rainbowSimilarity": round(float(row[0]), 6)})
    return records


def events(frames: list[dict]) -> list[dict]:
    by_day = {}
    for frame in frames:
        by_day.setdefault(frame["observedAt"][:10], []).append(frame)
    selected = []
    for day, group in by_day.items():
        day_selected = []
        for frame in sorted(group, key=lambda item: item["semanticScore"], reverse=True):
            when = datetime.fromisoformat(frame["observedAt"].replace("Z", "+00:00"))
            if any(abs((when - datetime.fromisoformat(old["observedAt"].replace("Z", "+00:00"))).total_seconds()) < 30 * 60 for old in day_selected):
                continue
            nearby = [item for item in group if abs((when - datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))).total_seconds()) <= 10 * 60]
            scores = sorted((item["semanticScore"] for item in nearby), reverse=True)
            day_selected.append({**frame, "persistentFrames": sum(score > 0 for score in scores),
                "secondBestScore": scores[1] if len(scores) > 1 else -1,
                "eventScore": round(frame["semanticScore"] + .35 * (scores[1] if len(scores) > 1 else -1), 6)})
            if len(day_selected) >= 2:
                break
        selected.extend(day_selected)
    return sorted(selected, key=lambda item: item["eventScore"], reverse=True)


def save(scanned: list[str], frames: list[dict]) -> None:
    OUTPUT.write_text(json.dumps({"schemaVersion": 1, "sampleIntervalRealMinutes": 5, "model": MODEL,
        "prompts": PROMPTS, "scannedMovies": scanned, "frames": frames, "events": events(frames)}, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    prior = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    scanned, frames = prior.get("scannedMovies", []), prior.get("frames", [])
    movies = sorted(MOVIES.glob("enaasimovieC1.a1.*.mpg"))
    remaining = [path for path in movies if path.name not in scanned]
    if args.limit:
        remaining = remaining[:args.limit]
    processor = AutoProcessor.from_pretrained(MODEL)
    model = CLIPModel.from_pretrained(MODEL).eval()
    text_inputs = processor(text=list(PROMPTS), return_tensors="pt", padding=True)
    with torch.inference_mode():
        text_vectors = normalize(model.get_text_features(**text_inputs)).cpu()
    known = reviewed_times()
    for number, path in enumerate(remaining, 1):
        frames.extend(scan_movie(path, processor, model, text_vectors, known)); scanned.append(path.name)
        if number % 10 == 0 or number == len(remaining):
            save(scanned, frames)
            print(f"Scanned {number}/{len(remaining)} remaining; {len(scanned)}/{len(movies)} total; {len(frames)} retained frames", flush=True)
    save(scanned, frames)
    print(OUTPUT)


if __name__ == "__main__":
    main()
