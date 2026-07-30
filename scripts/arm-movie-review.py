#!/usr/bin/env python3
"""Dewarp an ARM ASI movie-screen sequence toward the anti-solar horizon."""

from __future__ import annotations

import argparse
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"
LAT, LON = 36.607322, -97.487643
VIEW_WIDTH, VIEW_HEIGHT = 900, 500
AZIMUTH_SPAN_DEG = 50.0
MIN_ELEVATION_DEG, MAX_ELEVATION_DEG = -3.0, 35.0
RAD = math.pi / 180


def image_time(path: Path) -> datetime:
    match = re.search(r"(20\d{6})[._T-]?(\d{6})", path.name)
    if not match:
        raise ValueError(f"No timestamp in {path.name}")
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def solar_elevation(date: datetime) -> float:
    days = date.timestamp() / 86400 - 0.5 + 2440588 - 2451545
    anomaly = RAD * (357.5291 + 0.98560028 * days)
    longitude = anomaly + RAD * (1.9148 * math.sin(anomaly) + 0.02 * math.sin(2 * anomaly) + 0.0003 * math.sin(3 * anomaly)) + RAD * 102.9372 + math.pi
    declination = math.asin(math.sin(RAD * 23.4397) * math.sin(longitude))
    right_ascension = math.atan2(math.sin(longitude) * math.cos(RAD * 23.4397), math.cos(longitude))
    hour_angle = RAD * (280.16 + 360.9856235 * days) + LON * RAD - right_ascension
    latitude = LAT * RAD
    return math.asin(math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)) / RAD


def geometry(image: Image.Image):
    return image.width / 2, image.height / 2, min(image.size) * 0.46875


def locate_sun(image: Image.Image, elevation: float):
    sample_size = 384
    small = image.resize((sample_size, sample_size)).convert("L").filter(ImageFilter.GaussianBlur(4))
    values = np.asarray(small, dtype=np.float32)
    yy, xx = np.indices(values.shape)
    cx, cy, radius = geometry(image)
    scale = sample_size / image.width
    expected = radius * (90 - elevation) / 90 * scale
    mask = np.abs(np.hypot(xx - cx * scale, yy - cy * scale) - expected) <= max(4, 35 * image.width / 1536 * scale)
    y, x = np.unravel_index(np.argmax(np.where(mask, values, -1)), values.shape)
    return x / scale, y / scale


def dewarp(image: Image.Image, anti_angle: float):
    cx, cy, fisheye_radius = geometry(image)
    azimuth = np.linspace(-AZIMUTH_SPAN_DEG, AZIMUTH_SPAN_DEG, VIEW_WIDTH)
    elevation = np.linspace(MAX_ELEVATION_DEG, MIN_ELEVATION_DEG, VIEW_HEIGHT)
    az_grid, el_grid = np.meshgrid(azimuth, elevation)
    radius = fisheye_radius * (90 - el_grid) / 90
    angle = anti_angle + np.radians(az_grid)
    source_x = np.clip(np.rint(cx + radius * np.cos(angle)), 0, image.width - 1).astype(int)
    source_y = np.clip(np.rint(cy + radius * np.sin(angle)), 0, image.height - 1).astype(int)
    return Image.fromarray(np.asarray(image.convert("RGB"))[source_y, source_x])


def panorama(path: Path):
    image = Image.open(path).convert("RGB")
    cx, cy, fisheye_radius = geometry(image)
    az_grid, el_grid = np.meshgrid(np.linspace(-180, 180, 1800), np.linspace(35, -3, 300))
    radius = fisheye_radius * (90 - el_grid) / 90
    source_x = np.clip(np.rint(cx + radius * np.cos(np.radians(az_grid))), 0, image.width - 1).astype(int)
    source_y = np.clip(np.rint(cy + radius * np.sin(np.radians(az_grid))), 0, image.height - 1).astype(int)
    view = Image.fromarray(np.asarray(image)[source_y, source_x])
    draw = ImageDraw.Draw(view)
    draw.rectangle((0, 0, 420, 30), fill=(0, 0, 0))
    draw.text((8, 7), image_time(path).strftime("%Y-%m-%d %H:%M:%S UTC — FULL 360° HORIZON"), fill="white", font=ImageFont.load_default())
    return view


def panel(path: Path):
    image = Image.open(path).convert("RGB")
    timestamp = image_time(path)
    elevation = solar_elevation(timestamp)
    sun_x, sun_y = locate_sun(image, elevation)
    cx, cy, _ = geometry(image)
    view = dewarp(image, math.atan2(sun_y - cy, sun_x - cx) + math.pi)
    draw = ImageDraw.Draw(view)
    label = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    draw.rectangle((0, 0, 310, 30), fill=(0, 0, 0))
    draw.text((8, 7), label, fill=(255, 255, 255), font=ImageFont.load_default())
    return view


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    source = VALIDATION / "movie-frames" / args.candidate
    frames = sorted(source.glob("*.jpg"), key=image_time)
    if not frames:
        raise SystemExit(f"No movie frames in {source}")
    panels = [panel(path) for path in frames]
    sheet = Image.new("RGB", (VIEW_WIDTH * 3, VIEW_HEIGHT * 2))
    positions = ((0, 0), (900, 0), (1800, 0), (450, 500), (1350, 500))
    for item, position in zip(panels, positions):
        sheet.paste(item, position)
    output = VALIDATION / "review" / f"{args.candidate}-horizon-screen.jpg"
    sheet.save(output, quality=90, optimize=True)
    print(output)
    panorama_sheet = Image.new("RGB", (1800, 300 * len(frames)))
    for index, item in enumerate(map(panorama, frames)):
        panorama_sheet.paste(item, (0, index * 300))
    panorama_output = VALIDATION / "review" / f"{args.candidate}-360-screen.jpg"
    panorama_sheet.save(panorama_output, quality=90, optimize=True)
    print(panorama_output)


if __name__ == "__main__":
    main()
