#!/usr/bin/env python3
"""Render full-resolution review candidates from the complete ARM movie scan."""
from __future__ import annotations
import argparse, html, importlib.util, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(r"C:\Users\jeffm\rainbow-finder"); ENA=ROOT/"validation"/"arm-ena"
REVIEW=ENA/"expansion-review"; STREAM="enaasiskyimageC1.a1"

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result); return result

base=module("review_base",ROOT/"scripts"/"arm-ena-review-batch.py")
scanner=module("ena_scanner",ROOT/"scripts"/"arm-ena-sunshower-scan.py")

def choose(count):
    data=json.loads((ENA/"movie-semantic-ranked.json").read_text(encoding="utf-8")); result=[]; days=set()
    for event in data["events"]:
        day=event["observedAt"][:10].replace("-","")
        if day in days: continue
        days.add(day); result.append({**event,"day":day})
        if len(result)>=count: break
    return result

def render(event,number,archive):
    center=datetime.fromisoformat(event["observedAt"].replace("Z","+00:00")).astimezone(timezone.utc)
    frames=[]; candidate=f"armx-{number:03d}"
    for index,(path,observed) in enumerate(base.selected_members(archive,center),1):
        filename=f"{candidate}-{index:02d}.jpg"; output=REVIEW/filename
        base.reviewer.panorama(path).save(output,quality=92,optimize=True)
        frames.append({"file":filename,"observedAt":observed.isoformat().replace("+00:00","Z")})
    return {"candidateId":candidate,**event,"frames":frames}

def write_page(items):
    articles=[]
    for item in items:
        figures="".join(f'<figure><a href="{x["file"]}" target="_blank"><img src="{x["file"]}"></a><figcaption>{html.escape(x["observedAt"])}</figcaption></figure>' for x in item["frames"])
        facts=f'{item["candidateId"]} · {item["observedAt"]} · sun {item["sunElevationDeg"]:.1f}° · score {item["eventScore"]:.4f}'
        articles.append(f'<article><h2>{html.escape(facts)}</h2><div class="strip">{figures}</div></article>')
    page=REVIEW/"arm-expansion-review-01.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ARM Azores expansion</title><style>
body{background:#101114;color:#eee;font:16px system-ui;margin:0}main{max-width:1500px;margin:auto;padding:24px}article{background:#1b1d22;padding:16px;margin-bottom:30px}.strip{display:flex;gap:10px;overflow-x:auto}figure{flex:0 0 1100px;margin:0}img{display:block;width:1100px;height:auto;background:#000}figcaption{padding:5px;color:#bbb}h2{font-size:17px}</style></head><body><main><h1>ARM Azores · expanded semantic review</h1><p>Independent days from the complete movie scan. Five full-resolution 360° horizon panoramas per candidate: −10, −5, 0, +5, +10 minutes.</p>'''+"".join(articles)+"</main></body></html>",encoding="utf-8")
    (REVIEW/"arm-expansion-review-01.json").write_text(json.dumps(items,indent=2)+"\n",encoding="utf-8")
    return page

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--count",type=int,default=60); args=parser.parse_args()
    REVIEW.mkdir(parents=True,exist_ok=True); events=choose(args.count); files=scanner.file_index(STREAM,5000); auth=scanner.credentials()
    missing=[x["day"] for x in events if x["day"] not in files]
    if missing: raise RuntimeError(f"Missing full-resolution days: {', '.join(missing)}")
    archives={}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures={pool.submit(base.download_archive,files[x["day"]],auth):x["day"] for x in events}
        for number,future in enumerate(as_completed(futures),1):
            day=futures[future]; archives[day]=future.result()
            if number%10==0 or number==len(futures): print(f"Downloaded {number}/{len(futures)} full-resolution days",flush=True)
    rendered=[]
    for number,event in enumerate(events,1):
        rendered.append(render(event,number,archives[event["day"]]))
        if number%10==0 or number==len(events): print(f"Rendered {number}/{len(events)} candidates",flush=True)
    print(write_page(rendered))

if __name__=="__main__": main()
