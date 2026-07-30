#!/usr/bin/env python3
"""Build a horizontal human-review page from extracted GSFC panoramas."""
from __future__ import annotations

import html
import json
import shutil
from datetime import datetime
from pathlib import Path

from PIL import Image


ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
SOURCE = ROOT / "validation" / "gsfc" / "extracted" / "All-Sky_Unwrap" / "2024" / "20240402"
REVIEW = ROOT / "validation" / "gsfc" / "review"
CENTERS = [
    ("gsfc-01", datetime(2024, 4, 2, 19, 10), "strong local rain + sunlight; sun elevation 3.5°"),
    ("gsfc-02", datetime(2024, 4, 2, 18, 16), "nearby drizzle + sunlight; sun elevation 14.0°"),
    ("gsfc-03", datetime(2024, 4, 2, 17, 54), "nearby drizzle + sunlight; sun elevation 18.3°"),
    ("gsfc-04", datetime(2024, 4, 2, 17, 28), "nearby drizzle + sunlight; sun elevation 23.2°"),
]


def timestamp(path: Path) -> datetime:
    return datetime.strptime(path.stem.removeprefix("All_Sky_Unwrap_"), "%Y-%m-%d_%H%M%S")


def main() -> None:
    REVIEW.mkdir(parents=True, exist_ok=True)
    available = sorted(SOURCE.glob("*.jpg"), key=timestamp)
    sequences = []
    cards = []
    overviews = []
    for candidate_id, center, reason in CENTERS:
        nearest = sorted(available, key=lambda path: abs((timestamp(path) - center).total_seconds()))[:5]
        nearest.sort(key=timestamp)
        frames = []
        figures = []
        for index, source in enumerate(nearest, 1):
            destination = REVIEW / f"{candidate_id}-{index:02d}.jpg"
            shutil.copy2(source, destination)
            with Image.open(destination) as image:
                image.verify()
            observed = timestamp(source).isoformat()
            frames.append({"observedAtLocal": observed, "file": destination.name, "source": str(source)})
            figures.append(
                f'<figure><a href="{destination.name}" target="_blank"><img src="{destination.name}" '
                f'alt="{candidate_id} at {observed}"></a><figcaption>{observed[11:]}</figcaption></figure>'
            )
        sequences.append({"candidateId": candidate_id, "centerLocal": center.isoformat(), "reason": reason, "frames": frames})
        representative = frames[len(frames) // 2]["file"]
        overviews.append(
            f'<a class="overview-card" href="#{candidate_id}"><strong>{candidate_id}</strong>'
            f'<img src="{representative}" alt="{candidate_id} representative frame">'
            f'<span>{html.escape(center.strftime("%I:%M %p"))}</span></a>'
        )
        cards.append(
            f'<article id="{candidate_id}"><h2>{candidate_id} · {html.escape(center.strftime("April 2, 2024 %I:%M %p"))}</h2>'
            f'<p>{html.escape(reason)}. Click any panorama for full resolution.</p>'
            f'<div class="strip">{"".join(figures)}</div></article>'
        )

    page = REVIEW / "gsfc-review-batch-01.html"
    page.write_text("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>NASA GSFC rainbow candidates</title><style>
body{background:#101114;color:#eee;font:16px system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}
article{background:#1b1d22;padding:18px;margin:0 0 34px;border-radius:8px}.strip{display:flex;gap:12px;overflow-x:auto;padding-bottom:12px}
figure{flex:0 0 1050px;margin:0;background:#08090a}img{display:block;width:1050px;height:auto}figcaption{padding:6px 10px;color:#bbb}
h1{margin-top:0}h2{font-size:20px;margin-bottom:4px}p{color:#cfd2d8}.overview{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0 36px}
.overview-card{background:#1b1d22;color:#fff;text-decoration:none;padding:12px;border-radius:8px}.overview-card img{width:100%;margin:8px 0}.overview-card span{color:#bbb}
@media(max-width:800px){figure,img{width:850px;flex-basis:850px}.overview{grid-template-columns:1fr}}
</style></head><body><main><h1>NASA GSFC · targeted rainbow review</h1>
<p>Four five-frame horizontal sequences, selected from measured sunlight near rain. Label each rainbow, no rainbow, or uncertain.</p><div class="overview">"""
                    + "".join(overviews) + "</div>" + "".join(cards) + "</main></body></html>", encoding="utf-8")
    (REVIEW / "gsfc-review-batch-01.json").write_text(json.dumps(sequences, indent=2) + "\n", encoding="utf-8")
    print(page)


if __name__ == "__main__":
    main()
