#!/usr/bin/env python3
"""Collect and rank FAA images before the systematic production-review cutoff.

This is a local validation job. It never calls the live review API or event store.
It reuses the targeting and scoring functions from faa-rainbow-pilot.py, but writes
to a separate output directory and excludes previously collected ranked batches.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PILOT_PATH = Path(__file__).with_name("faa-rainbow-pilot.py")


def load_pilot():
    spec = importlib.util.spec_from_file_location("faa_rainbow_pilot", PILOT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {PILOT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_excluded(paths: list[Path]) -> list[dict]:
    excluded = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        excluded.extend(payload if isinstance(payload, list) else payload.get("events", []))
    return excluded


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="UTC ISO start, inclusive")
    parser.add_argument("--end", required=True, help="UTC ISO end, exclusive")
    parser.add_argument("--output-dir", type=Path, required=True, help="Separate local output directory")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-per-site", type=int, default=2)
    parser.add_argument("--exclude", action="append", type=Path, default=[])
    parser.add_argument("--batch", default="01")
    parser.add_argument("--skip-clip", action="store_true", help="Collect only; rank by metadata score when CLIP is unavailable")
    args = parser.parse_args()

    start, end = parse_utc(args.start), parse_utc(args.end)
    if end <= start:
        raise SystemExit("--end must be after --start")
    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output.mkdir(parents=True, exist_ok=True)

    pilot = load_pilot()
    pilot.OUT = output
    pilot.IMAGES = output / "images"
    pilot.REVIEW = output / "review"
    pilot.SITES_CACHE = output / "sites.json"
    pilot.ASOS_CACHE = output / "asos.csv"

    all_sites = pilot.get_sites()
    sites = [site for site in all_sites
             if site.get("country") == "US"
             and site.get("state") in pilot.WARM_STATES
             and site.get("siteActive") and site.get("cameras")]
    rows = pilot.get_asos(sites, start, end)
    excluded = load_excluded(args.exclude)
    events = pilot.target_events(sites, rows, limit=args.limit, max_per_site=args.max_per_site)
    old_keys = {(item.get("site", {}).get("siteId"), item.get("observedAt")) for item in excluded}
    events = [item for item in events if (item["site"]["siteId"], item["observedAt"]) not in old_keys]
    print(f"Targeted {len(events)} FAA events from {start.isoformat()} through {end.isoformat()}", flush=True)

    usable, all_frames = [], []
    for number, event in enumerate(events, 1):
        event["frames"] = pilot.image_sequence(event)
        if len(event["frames"]) >= 3:
            usable.append(event)
            all_frames.extend(event["frames"])
        print(f"Fetched FAA event {number}/{len(events)}; {len(usable)} usable", flush=True)

    if all_frames and not args.skip_clip:
        pilot.score_frames(all_frames)
    for event in usable:
        if args.skip_clip:
            for frame in event["frames"]:
                frame["semanticScore"] = None
                frame["quality"] = None
            event["finalScore"] = event["metadataScore"]
        else:
            scores = sorted((frame["semanticScore"] for frame in event["frames"]), reverse=True)
            qualities = sorted((frame["quality"] for frame in event["frames"]), reverse=True)
            event["finalScore"] = scores[0] + .35 * scores[min(1, len(scores) - 1)] + .04 * qualities[0] + .005 * event["metadataScore"]
    usable.sort(key=lambda event: event["finalScore"], reverse=True)

    ranked = output / "faa-ranked-retrospective.json"
    ranked.write_text(json.dumps(usable, default=str, indent=2) + "\n", encoding="utf-8")
    review = pilot.build_review(usable, output / "review", args.batch, "faaretro", "FAA pre-review retrospective batch")
    print(json.dumps({"ranked": str(ranked), "review": str(review), "targeted": len(events), "usable": len(usable), "frames": len(all_frames)}, indent=2))


if __name__ == "__main__":
    main()
