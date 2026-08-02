#!/usr/bin/env python3
"""Build a smaller local FAA review page from an existing ranked batch."""
from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path


def choose(items: list[dict], top: int = 20, per_band: int = 5) -> list[tuple[dict, str]]:
    selected: list[tuple[dict, str]] = [(item, "priority") for item in items[:top]]
    remaining = items[top:]
    band_size = max(1, len(remaining) // 3)
    for band_number in range(3):
        band = remaining[band_number * band_size:(band_number + 1) * band_size if band_number < 2 else None]
        selected.extend((item, f"calibration-band-{band_number + 1}") for item in band[:per_band])
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranked", type=Path, required=True)
    parser.add_argument("--source-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    items = json.loads(args.ranked.read_text(encoding="utf-8"))
    selected = choose(items)
    args.output.mkdir(parents=True, exist_ok=True)
    articles = []
    manifest = []
    for number, (item, tier) in enumerate(selected, 1):
        candidate_id = f"priority-{number:02d}"
        figures = []
        frames = []
        for index, frame in enumerate(item["frames"], 1):
            source_name = f"faaretro-{items.index(item) + 1:02d}-{index:02d}.jpg"
            filename = f"{candidate_id}-{index:02d}.jpg"
            shutil.copy2(args.source_review / source_name, args.output / filename)
            frame_id = f"{candidate_id}-{index:02d}"
            figures.append(f'<figure><img src="{filename}"><button type="button" class="rainbow-toggle" data-frame-id="{frame_id}">Mark rainbow</button><figcaption>{html.escape(frame["observedAt"])}</figcaption></figure>')
            frames.append({"frameId": frame_id, "file": filename, "observedAt": frame["observedAt"], "sourceUrl": frame.get("sourceUrl")})
        facts = f'{candidate_id} · {item["site"]["siteName"]}, {item["site"].get("state", "")} · {item["observedAt"]} · score {item["finalScore"]:.4f} · {tier}'
        articles.append(f'<article><h2>{html.escape(facts)}</h2><div class="strip">{"".join(figures)}</div></article>')
        manifest.append({"candidateId": candidate_id, "tier": tier, "sourceCandidate": items.index(item) + 1, "site": item["site"]["siteName"], "observedAt": item["observedAt"], "score": item["finalScore"], "frames": frames})
    key = "faa-priority-labels"
    page = args.output / "faa-priority-review.html"
    page.write_text('''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FAA priority rainbow review</title><style>body{background:#101114;color:#eee;font:16px system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}article{background:#1b1d22;padding:16px;margin-bottom:30px}.strip{display:flex;gap:10px;overflow-x:auto}figure{flex:0 0 520px;margin:0}img{width:520px;height:340px;object-fit:contain;background:#000}figcaption{color:#bbb;padding:5px}button{background:#303640;color:#fff;border:1px solid #687386;border-radius:5px;padding:7px 10px;margin-top:8px;cursor:pointer}.selected{background:#16834b;border-color:#51d88b}</style><main><h1>FAA priority rainbow review</h1><p>Mark individual frames that show a rainbow. Labels save in this browser.</p><button id="export" type="button">Export labels</button>''' + "".join(articles) + '''</main><script>const key="''' + key + '''";let labels=JSON.parse(localStorage.getItem(key)||"{}");const buttons=[...document.querySelectorAll(".rainbow-toggle")];function paint(b){const on=Boolean(labels[b.dataset.frameId]);b.classList.toggle("selected",on);b.textContent=on?"Rainbow marked":"Mark rainbow"}function save(){localStorage.setItem(key,JSON.stringify(labels))}buttons.forEach(b=>{paint(b);b.onclick=()=>{const id=b.dataset.frameId;if(labels[id])delete labels[id];else labels[id]={frameId:id,label:"rainbow"};save();paint(b)}});document.getElementById("export").onclick=()=>{const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([JSON.stringify(Object.values(labels),null,2)+"\n"],{type:"application/json"}));a.download=key+".json";a.click()};</script>''', encoding="utf-8")
    (args.output / "faa-priority-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"page": str(page), "events": len(selected), "frames": sum(len(item["frames"]) for item, _ in selected), "tiers": {tier: sum(1 for _, value in selected if value == tier) for tier in sorted({value for _, value in selected})}}, indent=2))


if __name__ == "__main__":
    main()
