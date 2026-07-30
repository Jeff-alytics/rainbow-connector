#!/usr/bin/env python3
"""Test CLIP rainbow ranking against IEM's documented Pella double rainbow."""
from __future__ import annotations
import html, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests, torch
from PIL import Image
from transformers import AutoProcessor, CLIPModel

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
OUT=ROOT/"validation"/"iem"; IMAGES=OUT/"control-images-utc"
API="https://mesonet.agron.iastate.edu/geojson/webcam.geojson"
MODEL="openai/clip-vit-base-patch32"
CONTROL=datetime(2005,5,27,0,15,tzinfo=timezone.utc)
PROMPTS=["a photograph of a natural atmospheric rainbow in the sky",
         "a cloudy sky without a rainbow","sun glare or lens flare",
         "a colorful camera artifact"]

def normalize(value):
    tensor=value if isinstance(value,torch.Tensor) else value.pooler_output
    return tensor/tensor.norm(dim=-1,keepdim=True)

def records_at(date):
    response=requests.get(API,params={"network":"KCCI","valid":date.isoformat().replace("+00:00","Z")},
                          timeout=60,headers={"User-Agent":"rainbow-connector-validation/1.0"})
    response.raise_for_status()
    return response.json()["features"]

def download(record,date):
    props=record["properties"]; url=props.get("imgurl") or props["url"]
    path=IMAGES/f'{props["cid"]}_{date.strftime("%Y%m%d%H%M")}.jpg'
    if not path.exists():
        response=requests.get(url,timeout=60,headers={"User-Agent":"rainbow-connector-validation/1.0"})
        if response.status_code==404: return None
        response.raise_for_status(); path.write_bytes(response.content)
    return path

def score(items):
    processor=AutoProcessor.from_pretrained(MODEL); model=CLIPModel.from_pretrained(MODEL).eval()
    text=processor(text=PROMPTS,return_tensors="pt",padding=True)
    with torch.inference_mode(): text_vectors=normalize(model.get_text_features(**text)).cpu()
    images=[Image.open(item["path"]).convert("RGB") for item in items]
    values=[]
    with torch.inference_mode():
        for start in range(0,len(images),16):
            inputs=processor(images=images[start:start+16],return_tensors="pt")
            values.append(normalize(model.get_image_features(**inputs)).cpu())
    similarities=torch.cat(values)@text_vectors.T
    for item,row in zip(items,similarities):
        item["semanticScore"]=round(float(row[0]-row[1:].max()),6)
        item["rainbowSimilarity"]=round(float(row[0]),6)

def write_page(items,name,title):
    cards=[]
    for rank,item in enumerate(sorted(items,key=lambda x:x["semanticScore"],reverse=True),1):
        facts=f'Rank {rank} · score {item["semanticScore"]:.4f} · {item["cameraId"]} {item["cameraName"]} · {item["observedAt"]}'
        source=Path(item["path"]).relative_to(OUT).as_posix()
        cards.append(f'<article><h2>{html.escape(facts)}</h2><img src="{html.escape(source)}"></article>')
    page=OUT/name
    page.write_text(f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(title)}</title><style>body{{background:#111;color:#eee;font:16px system-ui;margin:0}}
main{{max-width:1200px;margin:auto;padding:24px}}article{{background:#1d1d1d;padding:18px;margin-bottom:32px}}
img{{max-width:100%;height:auto}}h2{{font-size:18px}}</style></head><body><main><h1>{html.escape(title)}</h1>
{''.join(cards)}</main></body></html>''',encoding="utf-8")
    return page

def main():
    IMAGES.mkdir(parents=True,exist_ok=True)
    cross=[]
    for feature in records_at(CONTROL):
        props=feature["properties"]; path=download(feature,CONTROL)
        if path is None: continue
        cross.append({"cameraId":props["cid"],"cameraName":props["name"],
                      "observedAt":CONTROL.isoformat().replace("+00:00","Z"),"path":str(path)})
    sequence=[]
    for offset in range(-30,31,5):
        date=CONTROL+timedelta(minutes=offset)
        feature=next(x for x in records_at(date) if x["properties"]["cid"]=="KCCI-006")
        props=feature["properties"]; path=download(feature,date)
        if path is None: continue
        sequence.append({"cameraId":"KCCI-006","cameraName":"Pella",
                         "observedAt":date.isoformat().replace("+00:00","Z"),"path":str(path)})
    score(cross); score(sequence)
    data={"schemaVersion":1,"documentedPositive":{"cameraId":"KCCI-006","observedAt":"2005-05-27T00:15:00Z"},
          "crossCamera":cross,"pellaSequence":sequence}
    (OUT/"iem-control-results.json").write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")
    cross_page=write_page(cross,"iem-control-cross-camera.html","IEM documented-rainbow cross-camera control")
    sequence_page=write_page(sequence,"iem-control-pella-sequence.html","IEM Pella rainbow time sequence")
    ranked=sorted(cross,key=lambda x:x["semanticScore"],reverse=True)
    positive_rank=next(i for i,x in enumerate(ranked,1) if x["cameraId"]=="KCCI-006")
    print(f"Pella documented positive rank: {positive_rank}/{len(ranked)}")
    print("Pella control score:",next(x["semanticScore"] for x in cross if x["cameraId"]=="KCCI-006"))
    print("Sequence best:",max(sequence,key=lambda x:x["semanticScore"]))
    print(cross_page); print(sequence_page)

if __name__=="__main__": main()
