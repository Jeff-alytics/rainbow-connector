#!/usr/bin/env python3
"""Create a blinded IEM rainbow review batch from rain + low-sun webcam imagery."""
from __future__ import annotations
import csv, html, io, json, math, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests, torch
from PIL import Image, ImageOps, ImageDraw
from transformers import AutoProcessor, CLIPModel

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
OUT=ROOT/"validation"/"iem"; IMAGES=OUT/"batch-images"; REVIEW=OUT/"review"
ASOS="https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
WEBCAMS="https://mesonet.agron.iastate.edu/geojson/webcam.geojson"
MODEL="openai/clip-vit-base-patch32"; NETWORKS=("KCCI","KCRG")
PROMPTS=["a photograph of a natural atmospheric rainbow in the sky",
         "a cloudy sky without a rainbow","sun glare or lens flare",
         "a colorful camera artifact"]

def solar_elevation(date,lat=42.0,lon=-93.5):
    rad=math.pi/180; days=date.timestamp()/86400-.5+2440588-2451545
    anomaly=rad*(357.5291+.98560028*days)
    longitude=anomaly+rad*(1.9148*math.sin(anomaly)+.02*math.sin(2*anomaly)+.0003*math.sin(3*anomaly))+rad*102.9372+math.pi
    dec=math.asin(math.sin(rad*23.4397)*math.sin(longitude))
    ra=math.atan2(math.sin(longitude)*math.cos(rad*23.4397),math.cos(longitude))
    ha=rad*(280.16+360.9856235*days)+lon*rad-ra; latitude=lat*rad
    return math.asin(math.sin(latitude)*math.sin(dec)+math.cos(latitude)*math.cos(dec)*math.cos(ha))/rad

def rain_windows():
    params={"station":["DSM","OTM","ALO","CID","IOW","AMW","MIW","PEA","FOD","MCW","SUX","DBQ"],
            "data":["p01i","wxcodes"],"report_type":"3","sts":"2024-01-01T00:00:00Z",
            "ets":"2026-07-25T23:59:59Z","tz":"Etc/UTC","format":"onlycomma","missing":"empty"}
    response=requests.get(ASOS,params=params,timeout=300,
                          headers={"User-Agent":"rainbow-connector-validation/1.0"})
    response.raise_for_status(); buckets=defaultdict(set)
    for row in csv.DictReader(io.StringIO(response.text)):
        weather=(row.get("wxcodes") or "").upper()
        try: precip=float(row.get("p01i") or 0)
        except ValueError: precip=0
        if "RA" not in weather and precip<=0: continue
        date=datetime.strptime(row["valid"],"%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if not 2<solar_elevation(date)<25: continue
        minute=(date.minute//15)*15; bucket=date.replace(minute=minute,second=0)
        buckets[bucket].add(row["station"])
    by_day={}
    for date,stations in buckets.items():
        key=date.date()
        if key not in by_day or len(stations)>len(by_day[key][1]): by_day[key]=(date,stations)
    return [x[0] for x in sorted(by_day.values(),key=lambda x:len(x[1]),reverse=True)[:80]]

def webcam_features(date,network):
    response=requests.get(WEBCAMS,params={"network":network,"valid":date.isoformat().replace("+00:00","Z")},
                          timeout=60,headers={"User-Agent":"rainbow-connector-validation/1.0"})
    response.raise_for_status(); return response.json().get("features",[])

def download(feature,date):
    props=feature["properties"]; url=props.get("imgurl") or props.get("url")
    if not url: return None
    path=IMAGES/f'{props["cid"]}_{date.strftime("%Y%m%dT%H%MZ")}.jpg'
    if not path.exists():
        response=requests.get(url,timeout=60,headers={"User-Agent":"rainbow-connector-validation/1.0"})
        if response.status_code==404: return None
        response.raise_for_status(); path.write_bytes(response.content)
    try:
        with Image.open(path) as image: image.verify()
    except Exception: return None
    return path

def normalize(value):
    tensor=value if isinstance(value,torch.Tensor) else value.pooler_output
    return tensor/tensor.norm(dim=-1,keepdim=True)

class Scorer:
    def __init__(self):
        self.processor=AutoProcessor.from_pretrained(MODEL)
        self.model=CLIPModel.from_pretrained(MODEL).eval()
        text=self.processor(text=PROMPTS,return_tensors="pt",padding=True)
        with torch.inference_mode(): self.text=normalize(self.model.get_text_features(**text)).cpu()
    def score(self,items):
        images=[Image.open(x["path"]).convert("RGB") for x in items]; outputs=[]
        with torch.inference_mode():
            for start in range(0,len(images),16):
                inputs=self.processor(images=images[start:start+16],return_tensors="pt")
                outputs.append(normalize(self.model.get_image_features(**inputs)).cpu())
        similarities=torch.cat(outputs)@self.text.T
        for item,row in zip(items,similarities):
            item["semanticScore"]=round(float(row[0]-row[1:].max()),6)
            item["rainbowSimilarity"]=round(float(row[0]),6)

def main():
    IMAGES.mkdir(parents=True,exist_ok=True); REVIEW.mkdir(parents=True,exist_ok=True)
    windows=rain_windows(); candidates=[]
    for number,date in enumerate(windows,1):
        for network in NETWORKS:
            for feature in webcam_features(date,network):
                path=download(feature,date)
                if path is None: continue
                props=feature["properties"]
                candidates.append({"cameraId":props["cid"],"cameraName":props["name"],"network":network,
                    "observedAt":date.isoformat().replace("+00:00","Z"),"path":str(path),
                    "longitude":feature["geometry"]["coordinates"][0],"latitude":feature["geometry"]["coordinates"][1]})
        print(f"Downloaded window {number}/{len(windows)}; {len(candidates)} images",flush=True)
        time.sleep(.05)
    scorer=Scorer(); scorer.score(candidates)
    raw=sorted(candidates,key=lambda x:x["semanticScore"],reverse=True)
    sequences=[]; seen=set()
    for candidate in raw:
        if len(sequences)>=25: break
        key=(candidate["cameraId"],candidate["observedAt"])
        if key in seen: continue
        seen.add(key)
        center=datetime.fromisoformat(candidate["observedAt"].replace("Z","+00:00"))
        frames=[]
        for offset in (-10,-5,0,5,10):
            date=center+timedelta(minutes=offset)
            feature=next((x for x in webcam_features(date,candidate["network"])
                          if x["properties"]["cid"]==candidate["cameraId"]),None)
            if feature:
                path=download(feature,date)
                if path: frames.append({"cameraId":candidate["cameraId"],"cameraName":candidate["cameraName"],
                    "network":candidate["network"],"observedAt":date.isoformat().replace("+00:00","Z"),"path":str(path)})
        if len(frames)<3: continue
        scorer.score(frames); scores=sorted((x["semanticScore"] for x in frames),reverse=True)
        sequences.append({**candidate,"frames":frames,"peakScore":scores[0],
                          "secondScore":scores[1],"positiveFrames":sum(x>0 for x in scores)})
    sequences.sort(key=lambda x:(x["peakScore"]+min(x["positiveFrames"],3)*.004+x["secondScore"]*.2),reverse=True)
    selected=sequences[:10]; cards=[]
    for number,item in enumerate(selected,1):
        cid=f"iem-{number:02d}"; opened=[Image.open(x["path"]).convert("RGB") for x in item["frames"]]
        resized=[ImageOps.contain(x,(640,360)) for x in opened]
        sheet=Image.new("RGB",(sum(x.width for x in resized),max(x.height for x in resized)),(0,0,0)); x=0
        for image in resized: sheet.paste(image,(x,0)); x+=image.width
        draw=ImageDraw.Draw(sheet); draw.rectangle((0,0,min(sheet.width,640),28),fill="black")
        draw.text((8,7),f'{cid} {item["cameraId"]} {item["observedAt"]}',fill="white")
        image_name=f"{cid}-sequence.jpg"; sheet.save(REVIEW/image_name,quality=91,optimize=True)
        item["candidateId"]=cid; item["image"]=image_name
        facts=f'{cid} · {item["cameraName"]} · {item["observedAt"]} · peak {item["peakScore"]:.4f} · {item["positiveFrames"]}/{len(item["frames"])} positive frames'
        cards.append(f'<article><h2>{html.escape(facts)}</h2><img src="{image_name}"></article>')
    page=REVIEW/"iem-review-batch-01.html"
    page.write_text('''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>IEM blinded rainbow batch</title><style>body{background:#111;color:#eee;font:16px system-ui;margin:0}
main{max-width:1300px;margin:auto;padding:24px}article{background:#1d1d1d;padding:18px;margin-bottom:40px}
img{width:100%;height:auto}h2{font-size:18px}</style></head><body><main><h1>IEM blinded rainbow candidates</h1>
<p>Five-frame sequences from Iowa rain reports during low sun. Label rainbow, no rainbow, or uncertain.</p>'''+
        "".join(cards)+"</main></body></html>",encoding="utf-8")
    (OUT/"iem-batch-results.json").write_text(json.dumps({"windows":len(windows),"images":len(candidates),
        "ranked":raw,"sequences":sequences},indent=2)+"\n",encoding="utf-8")
    (REVIEW/"iem-review-batch-01.json").write_text(json.dumps(selected,indent=2)+"\n",encoding="utf-8")
    print(page)

if __name__=="__main__": main()
