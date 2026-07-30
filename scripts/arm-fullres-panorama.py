#!/usr/bin/env python3
"""Create a five-time full-360 horizon sheet from extracted ARM JPEGs."""

from __future__ import annotations

import argparse
import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
VALIDATION = ROOT / "validation" / "arm-lamont"
SPEC = importlib.util.spec_from_file_location("movie_review", ROOT / "scripts" / "arm-movie-review.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)

def image_time(path):
    match = re.search(r"(20\d{6})[._T-]?(\d{6})", path.name)
    if not match:
        raise ValueError(f"No timestamp in {path.name}")
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", type=int, required=True)
    parser.add_argument("--at", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    manifest = __import__("json").loads((VALIDATION / "manifest.json").read_text(encoding="utf-8"))
    pair = manifest["pairs"][args.pair - 1]
    source = VALIDATION / "frames" / pair["id"] / "event"
    available = sorted(source.glob("*.jpg"), key=image_time)
    if not available:
        raise SystemExit(f"No extracted frames in {source}")
    center = datetime.fromisoformat(args.at.replace("Z", "+00:00")).astimezone(timezone.utc)
    selected = [
        min(available, key=lambda path: abs((image_time(path) - center).total_seconds() - offset))
        for offset in (-600, -300, 0, 300, 600)
    ]
    sheet = Image.new("RGB", (1800, 1500))
    for index, path in enumerate(selected):
        sheet.paste(review.panorama(path), (0, index * 300))
    output = VALIDATION / "review" / f"{args.candidate}-360.jpg"
    sheet.save(output, quality=92, optimize=True)
    print(output)


if __name__ == "__main__":
    main()
