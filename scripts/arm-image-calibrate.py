#!/usr/bin/env python3
"""Calibrate simple rainbow-color features from human-labeled ARM ENA sequences."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
FRAMES = ROOT / "validation" / "arm-ena" / "frames"
LABELS = ROOT / "validation" / "arm-lamont" / "human-labels.json"
OUTPUT = ROOT / "validation" / "arm-ena" / "image-feature-calibration.json"


def tile_features(path):
    image = Image.open(path).convert("RGB").resize((512, 512), Image.Resampling.LANCZOS)
    rgb = np.asarray(image, dtype=float) / 255
    hsv = np.asarray(image.convert("HSV"), dtype=float) / 255
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    yy, xx = np.mgrid[:512, :512]
    radius = np.sqrt((xx - 255.5) ** 2 + (yy - 255.5) ** 2) / 512
    annulus = (radius >= 0.25) & (radius <= 0.49)
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    scores = []
    for size in (24, 40, 64):
        step = size // 2
        for y in range(0, 512 - size + 1, step):
            for x in range(0, 512 - size + 1, step):
                mask = annulus[y:y+size, x:x+size] & (sat[y:y+size, x:x+size] > 0.22) & (val[y:y+size, x:x+size] > 0.2)
                if mask.sum() < size * size * 0.12:
                    continue
                h = hue[y:y+size, x:x+size][mask]
                s = sat[y:y+size, x:x+size][mask]
                c = chroma[y:y+size, x:x+size][mask]
                groups = np.array([
                    np.mean((h < .08) | (h > .94)),
                    np.mean((h >= .08) & (h < .18)),
                    np.mean((h >= .18) & (h < .42)),
                    np.mean((h >= .42) & (h < .66)),
                    np.mean((h >= .66) & (h < .82)),
                    np.mean((h >= .82) & (h <= .94)),
                ])
                active = int(np.sum(groups > 0.025))
                warm = groups[0] + groups[1]
                green = groups[2]
                violet = groups[4] + groups[5]
                diversity = -float(np.sum(groups[groups > 0] * np.log(groups[groups > 0])))
                coexist = min(warm, green + violet)
                nonblue = warm + green + violet
                score = nonblue * (1 + active / 3) * (0.5 + diversity) * (0.5 + float(np.mean(s))) * (0.5 + coexist * 4)
                scores.append((score, nonblue, coexist, diversity, float(np.mean(c)), active))
    if not scores:
        return {"score": 0}
    scores.sort(reverse=True)
    best = scores[0]
    return {"score": round(best[0], 5), "nonBlue": round(best[1], 5),
            "coexist": round(best[2], 5), "diversity": round(best[3], 5),
            "chroma": round(best[4], 5), "activeHues": best[5]}


def main():
    labels = json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    rows = []
    for label in labels:
        match = re.match(r"ena-(\d+)-fullres", label["candidateId"])
        if not match or label["label"] not in ("rainbow", "no_rainbow"):
            continue
        directory = FRAMES / label["centerTime"].replace("-", "").replace(":", "").replace("Z", "Z")
        if not directory.exists():
            continue
        frames = []
        for path in sorted(directory.glob("*.jpg")):
            frames.append({"file": path.name, **tile_features(path)})
        if not frames:
            continue
        best = max(frames, key=lambda item: item["score"])
        rows.append({"candidateId": label["candidateId"], "label": label["label"],
                     "sequenceScore": best["score"], "bestFrame": best["file"], "frames": frames})
        print(label["candidateId"], label["label"], best["score"], flush=True)
    rows.sort(key=lambda item: item["sequenceScore"], reverse=True)
    OUTPUT.write_text(json.dumps({"schemaVersion": 1, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    positives = [row["sequenceScore"] for row in rows if row["label"] == "rainbow"]
    negatives = [row["sequenceScore"] for row in rows if row["label"] == "no_rainbow"]
    print(OUTPUT)
    print("positive", len(positives), min(positives), sum(positives)/len(positives), max(positives))
    print("negative", len(negatives), min(negatives), sum(negatives)/len(negatives), max(negatives))


if __name__ == "__main__":
    main()
