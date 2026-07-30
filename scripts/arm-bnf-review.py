#!/usr/bin/env python3
"""Render every valid ARM BNF low-sun event for human review."""
from __future__ import annotations

import html
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "arm-bnf"
REVIEW = OUT / "review"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


base = module("ena_review", ROOT / "scripts" / "arm-ena-review-batch.py")
scanner = module("bnf_scan", ROOT / "scripts" / "arm-bnf-sunshower-scan.py")
base.OUT, base.REVIEW = OUT, REVIEW


def page(items):
    articles = []
    for item in items:
        facts = (f'{item["matchingMinutes"]} matching rain+sun minutes · '
                 f'DNI {item["measuredDirectNormalIrradianceWm2"]:.0f} W/m² · '
                 f'rain {item["measuredRainRateMmHr"]:.2f} mm/hr · '
                 f'sun {item["sunElevationDeg"]:.1f}° · bow top {item["expectedRainbowTopElevationDeg"]:.1f}°')
        articles.append(f'<article><h2>{html.escape(item["candidateId"])} — {html.escape(item["observedAt"])}</h2>'
                        f'<p>{html.escape(facts)}</p><img src="{html.escape(item["image"])}"></article>')
    output = REVIEW / "bnf-review-batch-01.html"
    output.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Alabama rainbow review</title><style>body{margin:0;background:#111;color:#eee;font:16px/1.45 system-ui}main{max-width:1500px;margin:auto;padding:24px}article{background:#1d1d1d;padding:20px;margin-bottom:48px;border-radius:10px}p{color:#9fd}img{width:100%;height:auto}</style></head><body><main>
<h1>Alabama low-sun holdout</h1><p>Five full-resolution 360° panoramas per event: −10, −5, 0, +5, +10 minutes.</p>'''
                      + ''.join(articles) + '</main></body></html>', encoding="utf-8")
    return output


def main():
    events = json.loads((OUT / "surface-sunshower-ranked.json").read_text(encoding="utf-8"))["events"]
    asi = scanner.file_index(scanner.ASI, tar_only=True)
    auth = scanner.credentials()
    archives = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(base.download_archive, asi[event["day"]], auth): event["day"] for event in events}
        for future in as_completed(futures):
            archives[futures[future]] = future.result()
    REVIEW.mkdir(parents=True, exist_ok=True)
    rendered = []
    for number, event in enumerate(events, 1):
        item = base.render_candidate(number, event, archives[event["day"]])
        old = REVIEW / item["image"]
        candidate = f"bnf-{number:02d}"
        new = REVIEW / f"{candidate}-fullres-360.jpg"
        old.replace(new)
        item["candidateId"], item["image"] = candidate, new.name
        rendered.append(item)
        print(f"Rendered {candidate}", flush=True)
    manifest = REVIEW / "bnf-review-batch-01.json"
    manifest.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
    print(page(rendered))


if __name__ == "__main__":
    main()
