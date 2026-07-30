#!/usr/bin/env python3
"""Render the empirical ENA holdout selection with the verified batch renderer."""
from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "arm-ena"


def load_module():
    path = ROOT / "scripts" / "arm-ena-review-batch.py"
    spec = importlib.util.spec_from_file_location("review_batch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    base = load_module()
    events = json.loads((OUT / "targeted-batch-01.json").read_text(encoding="utf-8"))["events"][:20]
    asi = base.scanner.file_index(base.ASI, 2000)
    auth = base.scanner.credentials()
    unique = {event["day"]: asi[event["day"]] for event in events}
    archives = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(base.download_archive, info, auth): day for day, info in unique.items()}
        for number, future in enumerate(as_completed(futures), 1):
            day = futures[future]
            archives[day] = future.result()
            print(f"Downloaded {number}/{len(futures)}: {day}", flush=True)
    base.REVIEW.mkdir(parents=True, exist_ok=True)
    rendered = []
    for number, event in enumerate(events, 81):
        rendered.append(base.render_candidate(number, event, archives[event["day"]]))
        print(f"Rendered {number - 80}/{len(events)}: ena-{number:02d}", flush=True)
    page = base.write_page(rendered, 4)
    manifest = base.REVIEW / "ena-review-batch-04.json"
    manifest.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
    print(page)


if __name__ == "__main__":
    main()
