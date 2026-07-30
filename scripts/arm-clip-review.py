#!/usr/bin/env python3
"""Render a small full-resolution review batch from semantic rainbow rankings."""
from __future__ import annotations
import html, importlib.util, json
from datetime import datetime
from pathlib import Path
from PIL import Image

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
ENA=ROOT/"validation"/"arm-ena"; REVIEW=ENA/"review"
LABEL_FILES=[ROOT/"validation"/"arm-lamont"/"human-labels.json",
             ENA/"image-first-human-labels.json",ENA/"clip-human-labels.json"]

def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

base=load_module("ena_review",ROOT/"scripts"/"arm-ena-review-batch.py")

def nearby(event,frames):
    center=datetime.fromisoformat(event["observedAt"].replace("Z","+00:00"))
    return [x for x in frames if x["archive"]==event["archive"] and
            abs((datetime.fromisoformat(x["observedAt"].replace("Z","+00:00"))-center).total_seconds())<=600]

def labeled_times():
    result=[]
    for path in LABEL_FILES:
        result.extend(datetime.fromisoformat(x["centerTime"].replace("Z","+00:00"))
                      for x in json.loads(path.read_text(encoding="utf-8"))["labels"])
    return result

def main():
    data=json.loads((ENA/"clip-calibrated-ranked.json").read_text(encoding="utf-8"))
    known=labeled_times()
    enriched=[]
    for event in data["events"]:
        center=datetime.fromisoformat(event["observedAt"].replace("Z","+00:00"))
        if any(abs((center-item).total_seconds())<=3600 for item in known): continue
        scores=sorted((x["semanticScore"] for x in nearby(event,data["frames"])),reverse=True)
        enriched.append({**event,"persistentFrames":sum(x>0.02 for x in scores),
                         "secondBestScore":scores[1] if len(scores)>1 else -1})
    persistent=sorted((x for x in enriched if x["persistentFrames"]>=2),
                      key=lambda x:x["semanticScore"],reverse=True)
    selected=persistent[:10]
    REVIEW.mkdir(parents=True,exist_ok=True); rendered=[]
    for number,event in enumerate(selected,1):
        center=datetime.fromisoformat(event["observedAt"].replace("Z","+00:00"))
        archive=ENA/"downloads"/event["archive"]
        frames=base.selected_members(archive,center)
        sheet=Image.new("RGB",(1800,1500))
        for index,(path,_) in enumerate(frames):
            sheet.paste(base.reviewer.panorama(path),(0,index*300))
        candidate_id=f"calibrated-{number:02d}"
        image_name=f"{candidate_id}-fullres-360.jpg"
        sheet.save(REVIEW/image_name,quality=92,optimize=True)
        rendered.append({"candidateId":candidate_id,"image":image_name,**event})
        print(f"Rendered {candidate_id}: {event['observedAt']}",flush=True)
    articles=[]
    for item in rendered:
        facts=(f'semantic score {item["semanticScore"]:.4f} · '
               f'{item["persistentFrames"]} elevated frames within ±10 minutes · '
               f'sun {item["sunElevationDeg"]:.1f}°')
        articles.append(f'''<article><h2>{html.escape(item["candidateId"])} — {html.escape(item["observedAt"])}</h2>
<p>{html.escape(facts)}</p><img src="{html.escape(item["image"])}" alt="{html.escape(item["candidateId"])}"></article>''')
    page=REVIEW/"clip-review-batch-02.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Semantic rainbow candidates</title><style>body{background:#111;color:#eee;font:16px/1.45 system-ui;margin:0}
main{max-width:1500px;margin:auto;padding:24px}article{background:#1d1d1d;padding:20px;margin-bottom:48px}
p{color:#9fd}img{width:100%;height:auto}</style></head><body><main><h1>Semantic rainbow candidates</h1>
<p>Ten strongest calibrated, unseen events with elevated rainbow semantics in at least two sampled frames. Previously reviewed windows and the surrounding hour are excluded. Each image shows five full-resolution 360° horizon panoramas at −10, −5, 0, +5, and +10 minutes.</p>'''+
        "".join(articles)+"</main></body></html>",encoding="utf-8")
    (REVIEW/"clip-review-batch-02.json").write_text(json.dumps(rendered,indent=2)+"\n",encoding="utf-8")
    print(page)

if __name__=="__main__": main()
