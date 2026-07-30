#!/usr/bin/env python3
"""Render the top cached image-first ENA candidates for review."""
from __future__ import annotations

import html
import importlib.util
import json
from pathlib import Path

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
OUT=ROOT/"validation"/"arm-ena"
REVIEW=OUT/"review"


def module():
    path=ROOT/"scripts"/"arm-ena-review-batch.py"
    spec=importlib.util.spec_from_file_location("base_review",path)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


def main():
    base=module(); base.OUT=OUT; base.REVIEW=REVIEW
    events=json.loads((OUT/"image-first-cached-ranked.json").read_text(encoding="utf-8"))["events"][:20]
    REVIEW.mkdir(parents=True,exist_ok=True); rendered=[]
    for number,event in enumerate(events,1):
        archive=OUT/"downloads"/event["archive"]
        item=base.render_candidate(number,event,archive)
        old=REVIEW/item["image"]; cid=f"visual-{number:02d}"; new=REVIEW/f"{cid}-fullres-360.jpg"; old.replace(new)
        item["candidateId"],item["image"]=cid,new.name; rendered.append(item); print(f"Rendered {cid}",flush=True)
    articles=[]
    for item in rendered:
        facts=f'visual score {item["visualScore"]:.2f} · sun {item["sunElevationDeg"]:.1f}° · bow top {item["expectedRainbowTopElevationDeg"]:.1f}°'
        articles.append(f'<article><h2>{item["candidateId"]} — {item["observedAt"]}</h2><p>{html.escape(facts)}</p><img src="{item["image"]}"></article>')
    page=REVIEW/"image-first-review-batch-01.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Image-first rainbow review</title><style>body{background:#111;color:#eee;font:16px/1.45 system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}article{background:#1d1d1d;padding:20px;margin-bottom:48px}p{color:#9fd}img{width:100%}</style></head><body><main><h1>Image-first anti-solar candidates</h1><p>Five full-resolution 360° panoramas per event. These candidates do not require rain at the surface gauge.</p>'''+''.join(articles)+"</main></body></html>",encoding="utf-8")
    (REVIEW/"image-first-review-batch-01.json").write_text(json.dumps(rendered,indent=2)+"\n",encoding="utf-8")
    print(page)


if __name__=="__main__": main()
