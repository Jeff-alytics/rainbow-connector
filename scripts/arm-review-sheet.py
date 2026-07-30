#!/usr/bin/env python3
"""Create human-reviewable dewarped anti-solar views from ARM fisheye frames."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"
CX = CY = 768.0
FISHEYE_RADIUS = 720.0
VIEW_WIDTH = 900
VIEW_HEIGHT = 500
AZIMUTH_SPAN_DEG = 50.0
MIN_ELEVATION_DEG = -3.0
MAX_ELEVATION_DEG = 35.0
RAD = math.pi / 180


def image_time(path: Path) -> datetime:
    match = re.search(r"(20\d{6})[._-]?(\d{6})", path.name)
    if not match:
        raise ValueError(f"No timestamp in {path.name}")
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def solar_elevation(date: datetime, lat: float, lon: float) -> float:
    days = date.timestamp() * 1000 / 86_400_000 - 0.5 + 2440588 - 2451545
    anomaly = RAD * (357.5291 + 0.98560028 * days)
    longitude = anomaly + RAD * (
        1.9148 * math.sin(anomaly)
        + 0.02 * math.sin(2 * anomaly)
        + 0.0003 * math.sin(3 * anomaly)
    ) + RAD * 102.9372 + math.pi
    declination = math.asin(math.sin(RAD * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(
        math.sin(longitude) * math.cos(RAD * 23.4397), math.cos(longitude)
    )
    latitude = lat * RAD
    hour_angle = RAD * (280.16 + 360.9856235 * days) + lon * RAD - right_ascension
    return math.asin(
        math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)
    ) / RAD


def locate_sun(image: Image.Image, elevation_deg: float):
    small = image.resize((384, 384)).convert("L").filter(ImageFilter.GaussianBlur(4))
    values = np.asarray(small, dtype=np.float32)
    yy, xx = np.indices(values.shape)
    scale = 384 / 1536
    cx, cy = CX * scale, CY * scale
    expected_radius = FISHEYE_RADIUS * (90 - elevation_deg) / 90 * scale
    radius = np.hypot(xx - cx, yy - cy)
    mask = np.abs(radius - expected_radius) <= 35 * scale
    score = np.where(mask, values, -1)
    y, x = np.unravel_index(np.argmax(score), score.shape)
    return x / scale, y / scale


def dewarp(image: Image.Image, anti_angle: float):
    azimuth = np.linspace(-AZIMUTH_SPAN_DEG, AZIMUTH_SPAN_DEG, VIEW_WIDTH)
    elevation = np.linspace(MAX_ELEVATION_DEG, MIN_ELEVATION_DEG, VIEW_HEIGHT)
    az_grid, el_grid = np.meshgrid(azimuth, elevation)
    radius = FISHEYE_RADIUS * (90 - el_grid) / 90
    angle = anti_angle + np.radians(az_grid)
    source_x = np.clip(np.rint(CX + radius * np.cos(angle)), 0, image.width - 1).astype(int)
    source_y = np.clip(np.rint(CY + radius * np.sin(angle)), 0, image.height - 1).astype(int)
    pixels = np.asarray(image.convert("RGB"))
    return Image.fromarray(pixels[source_y, source_x])


def bow_points(sun_elevation: float):
    antisolar_elevation = -sun_elevation * RAD
    points = []
    for x in range(VIEW_WIDTH):
        delta = (-AZIMUTH_SPAN_DEG + 2 * AZIMUTH_SPAN_DEG * x / (VIEW_WIDTH - 1)) * RAD

        def separation(elevation_deg: float):
            elevation = elevation_deg * RAD
            dot = (
                math.sin(elevation) * math.sin(antisolar_elevation)
                + math.cos(elevation) * math.cos(antisolar_elevation) * math.cos(delta)
            )
            return math.acos(max(-1, min(1, dot))) / RAD

        low, high = 0.0, MAX_ELEVATION_DEG
        if separation(low) > 42 or separation(high) < 42:
            continue
        for _ in range(25):
            middle = (low + high) / 2
            if separation(middle) < 42:
                low = middle
            else:
                high = middle
        elevation = (low + high) / 2
        y = round(
            (MAX_ELEVATION_DEG - elevation)
            / (MAX_ELEVATION_DEG - MIN_ELEVATION_DEG)
            * (VIEW_HEIGHT - 1)
        )
        points.append((x, y))
    return points


def panel(path: Path, lat: float, lon: float, annotate: bool):
    image = Image.open(path).convert("RGB")
    timestamp = image_time(path)
    elevation = solar_elevation(timestamp, lat, lon)
    sun_x, sun_y = locate_sun(image, elevation)
    anti_angle = math.atan2(sun_y - CY, sun_x - CX) + math.pi
    view = dewarp(image, anti_angle)
    draw = ImageDraw.Draw(view)
    if annotate:
        points = bow_points(elevation)
        for start in range(0, len(points) - 1, 16):
            segment = points[start : start + 9]
            if len(segment) > 1:
                draw.line(segment, fill=(255, 225, 70), width=3)
    label = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    draw.rectangle((0, 0, 310, 30), fill=(0, 0, 0))
    draw.text((8, 7), label, fill=(255, 255, 255), font=ImageFont.load_default())
    return view


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=int, required=True)
    parser.add_argument("--at", required=True, help="UTC center time")
    parser.add_argument("--candidate", required=True, help="Review name, e.g. candidate-02")
    args = parser.parse_args()

    manifest = json.loads((VALIDATION / "manifest.json").read_text(encoding="utf-8"))
    pair = manifest["pairs"][args.pair - 1]
    center = datetime.fromisoformat(args.at.replace("Z", "+00:00")).astimezone(timezone.utc)
    directory = VALIDATION / "frames" / pair["id"] / "event"
    available = sorted(directory.glob("*.jpg"), key=image_time)
    offsets = (-360, -120, 120, 360)
    chosen = [
        min(available, key=lambda path: abs((image_time(path) - center).total_seconds() - offset))
        for offset in offsets
    ]
    review = VALIDATION / "review"
    review.mkdir(parents=True, exist_ok=True)
    for annotate, suffix in ((False, "dewarped"), (True, "annotated")):
        panels = [panel(path, manifest["site"]["lat"], manifest["site"]["lon"], annotate) for path in chosen]
        sheet = Image.new("RGB", (VIEW_WIDTH * 2, VIEW_HEIGHT * 2))
        for index, item in enumerate(panels):
            sheet.paste(item, ((index % 2) * VIEW_WIDTH, (index // 2) * VIEW_HEIGHT))
        output = review / f"{args.candidate}-{suffix}.jpg"
        sheet.save(output, quality=90, optimize=True)
        print(output)


if __name__ == "__main__":
    main()
