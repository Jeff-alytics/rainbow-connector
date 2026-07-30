#!/usr/bin/env python3
"""Render the single valid ARM NSA liquid low-sun event."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "arm-nsa"
REVIEW = OUT / "review"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


base = module("ena_review", ROOT / "scripts" / "arm-ena-review-batch.py")
scanner = module("cross_scan", ROOT / "scripts" / "arm-bnf-sunshower-scan.py")
scanner.OUT = OUT
scanner.ASI = "nsaasiskyimageC1.a1"
scanner.SIRS = "nsasirsC1.b1"
scanner.MET = "nsametC1.b1"
base.OUT, base.REVIEW = OUT, REVIEW


def main():
    event = json.loads((OUT / "surface-sunshower-ranked.json").read_text(encoding="utf-8"))["events"][0]
    asi = scanner.file_index(scanner.ASI, tar_only=True)
    archive = base.download_archive(asi[event["day"]], scanner.credentials())
    REVIEW.mkdir(parents=True, exist_ok=True)
    item = base.render_candidate(1, event, archive)
    old = REVIEW / item["image"]
    new = REVIEW / "nsa-01-fullres-360.jpg"
    old.replace(new)
    item["candidateId"], item["image"] = "nsa-01", new.name
    (REVIEW / "nsa-review-batch-01.json").write_text(json.dumps([item], indent=2) + "\n", encoding="utf-8")
    facts = (f'{event["matchingMinutes"]} matching minute; DNI {event["measuredDirectNormalIrradianceWm2"]:.0f} W/m²; '
             f'rain {event["measuredRainRateMmHr"]:.2f} mm/hr; sun {event["sunElevationDeg"]:.1f}°; '
             f'bow top {event["expectedRainbowTopElevationDeg"]:.1f}°')
    page = REVIEW / "nsa-review-batch-01.html"
    page.write_text(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Alaska rainbow holdout</title><style>body{{background:#111;color:#eee;font:16px/1.45 system-ui}}main{{max-width:1500px;margin:auto;padding:24px}}p{{color:#9fd}}img{{width:100%}}</style></head>
<body><main><h1>Alaska liquid low-sun holdout</h1><h2>nsa-01 — {event["observedAt"]}</h2><p>{facts}</p>
<img src="nsa-01-fullres-360.jpg"></main></body></html>''', encoding="utf-8")
    print(page)


if __name__ == "__main__":
    main()
