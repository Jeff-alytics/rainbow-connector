#!/usr/bin/env python3
"""Render IEM semantic sequence ranks 11-20 for human review."""
from __future__ import annotations
import html, json
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
OUT=ROOT/"validation"/"iem"; REVIEW=OUT/"review"

def render_batch(sequences,start,batch):
    selected=sequences[start:start+10]; cards=[]
    for number,item in enumerate(selected,start+1):
        cid=f"iem-{number:02d}"
        opened=[Image.open(x["path"]).convert("RGB") for x in item["frames"]]
        resized=[ImageOps.contain(x,(640,360)) for x in opened]
        sheet=Image.new("RGB",(sum(x.width for x in resized),max(x.height for x in resized)),(0,0,0)); x=0
        for image in resized: sheet.paste(image,(x,0)); x+=image.width
        draw=ImageDraw.Draw(sheet); draw.rectangle((0,0,min(sheet.width,640),28),fill="black")
        draw.text((8,7),f'{cid} {item["cameraId"]} {item["observedAt"]}',fill="white")
        image_name=f"{cid}-sequence.jpg"; sheet.save(REVIEW/image_name,quality=91,optimize=True)
        item["candidateId"]=cid; item["image"]=image_name
        facts=(f'{cid} · {item["cameraName"]} · {item["observedAt"]} · '
               f'peak {item["peakScore"]:.4f} · {item["positiveFrames"]}/{len(item["frames"])} positive frames')
        cards.append(f'<article><h2>{html.escape(facts)}</h2><img src="{image_name}"></article>')
    page=REVIEW/f"iem-review-batch-{batch:02d}.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>IEM blinded rainbow batch</title><style>body{background:#111;color:#eee;font:16px system-ui;margin:0}
main{max-width:2400px;margin:auto;padding:24px}article{background:#1d1d1d;padding:18px;margin-bottom:40px;overflow-x:auto}
img{display:block;max-width:none;height:auto}h2{font-size:18px}</style></head><body><main><h1>IEM blinded candidates</h1>
<p>Five-frame sequences from Iowa rain reports during low sun. Label rainbow, no rainbow, or uncertain.</p>'''+
        "".join(cards)+"</main></body></html>",encoding="utf-8")
    (REVIEW/f"iem-review-batch-{batch:02d}.json").write_text(json.dumps(selected,indent=2)+"\n",encoding="utf-8")
    print(page)

def main():
    data=json.loads((OUT/"iem-batch-results.json").read_text(encoding="utf-8"))
    render_batch(data["sequences"],0,1)
    render_batch(data["sequences"],10,2)

if __name__=="__main__": main()
