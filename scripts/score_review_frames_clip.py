#!/usr/bin/env python
"""Sealed second-reviewer: CLIP semantic rainbow scores for review-queue frames.

Scores every downloaded frame in validation/historical-review-queue/images with
the same model and contrastive prompts proven on the ARM sky-camera work
(scripts/arm-clip-cached-scan.py). Output is a machine-reviewer record that
stays SEALED until the human export lands — it must never be used to order or
filter the blind review page. Resumable; writes incrementally per event.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import torch
from PIL import Image
from transformers import CLIPModel, CLIPProcessor

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
QUEUE = ROOT / "validation" / "historical-review-queue"
OUTPUT = QUEUE / "machine-reviewer-clip.jsonl"
MODEL_NAME = "openai/clip-vit-base-patch32"
PROMPTS = ["a photograph of a natural atmospheric rainbow in the sky",
           "a cloudy sky without a rainbow", "sun glare or lens flare in a sky camera"]
BATCH = 16


def main() -> int:
    done = set()
    if OUTPUT.exists():
        for line in OUTPUT.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["eventId"])
            except (ValueError, KeyError):
                continue

    processor = CLIPProcessor.from_pretrained(MODEL_NAME)
    model = CLIPModel.from_pretrained(MODEL_NAME).eval()

    manifest = json.loads((QUEUE / "frames-manifest.json").read_text(encoding="utf-8"))
    events = [event for event in manifest["events"] if event.get("downloadedFrames")]
    pending = [event for event in events if event["eventId"] not in done]
    print(f"{len(events)} events with frames; {len(pending)} to score", flush=True)

    for number, event in enumerate(pending, 1):
        started = time.time()
        frames = []
        for frame in event["downloadedFrames"]:
            path = QUEUE / "images" / event["eventId"] / frame["file"]
            if not path.exists():
                continue
            try:
                image = Image.open(path).convert("RGB")
            except OSError:
                continue
            frames.append((frame["file"], frame.get("observedAt"), image))
        rows = []
        for offset in range(0, len(frames), BATCH):
            chunk = frames[offset:offset + BATCH]
            # transformers 5: run the joint forward; logits_per_image is
            # logit_scale * cosine similarity against each prompt.
            inputs = processor(text=PROMPTS, images=[image for _, _, image in chunk],
                               return_tensors="pt", padding=True)
            with torch.inference_mode():
                out = model(**inputs)
                scale = model.logit_scale.exp()
                similarities = (out.logits_per_image / scale).cpu()
                probabilities = torch.softmax(out.logits_per_image, dim=-1).cpu()
            for (name, observed_at, _), sim_row, prob_row in zip(chunk, similarities, probabilities):
                rows.append({"file": name, "observedAt": observed_at,
                             "rainbowSimilarity": round(float(sim_row[0]), 6),
                             "contrastiveRainbowProb": round(float(prob_row[0]), 6)})
        record = {"eventId": event["eventId"], "disposition": event.get("disposition"),
                  "frames": rows,
                  "maxRainbowSimilarity": max((r["rainbowSimilarity"] for r in rows), default=None),
                  "maxContrastiveProb": max((r["contrastiveRainbowProb"] for r in rows), default=None)}
        with OUTPUT.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        if number % 25 == 0 or number == len(pending):
            print(f"[{number}/{len(pending)}] {event['eventId']} frames={len(rows)} "
                  f"({time.time() - started:.1f}s)", flush=True)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
