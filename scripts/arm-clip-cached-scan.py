#!/usr/bin/env python3
"""Semantic rainbow scan of cached full-day ARM ENA sky-image archives."""
from __future__ import annotations
import importlib.util, io, json, math, tarfile
from datetime import datetime
from pathlib import Path
import torch
from PIL import Image
from transformers import AutoProcessor, CLIPModel

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
ENA=ROOT/"validation"/"arm-ena"; ARCHIVES=ENA/"downloads"
OUTPUT=ENA/"clip-calibrated-ranked.json"
LABELS=ROOT/"validation"/"arm-lamont"/"human-labels.json"
MODEL_NAME="openai/clip-vit-base-patch32"
PROMPTS=["a photograph of a natural atmospheric rainbow in the sky",
         "a cloudy sky without a rainbow","sun glare or lens flare in a sky camera",
         "a colorful camera lens artifact"]

def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

cached=load_module("cached_scan",ROOT/"scripts"/"arm-image-cached-scan.py")
review=load_module("movie_review",ROOT/"scripts"/"arm-movie-review.py")
review.LAT,review.LON=cached.LAT,cached.LON; review.MAX_ELEVATION_DEG=45.0

def normalize(value):
    tensor=value if isinstance(value,torch.Tensor) else value.pooler_output
    return tensor/tensor.norm(dim=-1,keepdim=True)

def panel(image,date):
    _,bearing=cached.solar(date)
    anti_angle=(cached.SIGN*((bearing+180)%360)+cached.OFFSET)%360
    return review.dewarp(image,math.radians(anti_angle))

def reviewed_times():
    labels=json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    return [datetime.fromisoformat(x["centerTime"].replace("Z","+00:00")) for x in labels
            if x["candidateId"].startswith(("ena-","visual-"))]

def save(scanned,frames):
    OUTPUT.write_text(json.dumps({"schemaVersion":1,"model":MODEL_NAME,"prompts":PROMPTS,
        "scannedArchives":scanned,"frames":frames},indent=2)+"\n",encoding="utf-8")

def scan_archive(path,processor,model,text_vectors,known):
    records=[]; panels=[]; metadata=[]
    def score_batch():
        if not panels: return
        inputs=processor(images=panels,return_tensors="pt")
        with torch.inference_mode(): vectors=normalize(model.get_image_features(**inputs)).cpu()
        similarities=vectors@text_vectors.T
        for item,row in zip(metadata,similarities):
            records.append({**item,"semanticScore":round(float(row[0]-row[1:].max()),6),
                            "rainbowSimilarity":round(float(row[0]),6)})
        panels.clear(); metadata.clear()
    with tarfile.open(path,"r") as bundle:
        for member in bundle.getmembers():
            date=cached.timestamp(member.name)
            if not member.isfile() or not date or date.minute%2 or date.second: continue
            elevation,_=cached.solar(date)
            if not 0<elevation<25: continue
            if any(abs((date-item).total_seconds())<=1200 for item in known): continue
            try:
                source=bundle.extractfile(member)
                image=Image.open(io.BytesIO(source.read())).convert("RGB")
                panels.append(panel(image,date))
                metadata.append({"observedAt":date.isoformat().replace("+00:00","Z"),
                    "archive":path.name,"member":member.name,"sunElevationDeg":round(elevation,2),
                    "expectedRainbowTopElevationDeg":round(42-elevation,2)})
                if len(panels)==16: score_batch()
            except Exception as error: print(f"Skipped {member.name}: {error}",flush=True)
        score_batch()
    return records

def ranked_events(frames):
    groups=[]
    for frame in sorted(frames,key=lambda x:x["observedAt"]):
        date=datetime.fromisoformat(frame["observedAt"].replace("Z","+00:00"))
        prior=datetime.fromisoformat(groups[-1][-1]["observedAt"].replace("Z","+00:00")) if groups else None
        if groups and (date-prior).total_seconds()<=600: groups[-1].append(frame)
        else: groups.append([frame])
    events=[]
    for group in groups:
        best=max(group,key=lambda x:x["semanticScore"])
        events.append({**best,"eventStart":group[0]["observedAt"],"eventEnd":group[-1]["observedAt"],
                       "screenedFrames":len(group)})
    return sorted(events,key=lambda x:x["semanticScore"],reverse=True)

def main():
    previous=json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    scanned=previous.get("scannedArchives",[]); frames=previous.get("frames",[])
    archives=sorted(ARCHIVES.glob("enaasiskyimageC1.a1.*.jpg.tar"))
    processor=AutoProcessor.from_pretrained(MODEL_NAME)
    model=CLIPModel.from_pretrained(MODEL_NAME).eval()
    text=processor(text=PROMPTS,return_tensors="pt",padding=True)
    with torch.inference_mode(): text_vectors=normalize(model.get_text_features(**text)).cpu()
    remaining=[path for path in archives if path.name not in scanned]; known=reviewed_times()
    for number,path in enumerate(remaining,1):
        frames.extend(scan_archive(path,processor,model,text_vectors,known)); scanned.append(path.name)
        save(scanned,frames)
        print(f"Scanned {number}/{len(remaining)} remaining; {len(scanned)}/{len(archives)} total archives; {len(frames)} frames",flush=True)
    events=ranked_events(frames); data=json.loads(OUTPUT.read_text(encoding="utf-8")); data["events"]=events
    OUTPUT.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")
    print(f"Wrote {len(events)} ranked events to {OUTPUT}",flush=True)
    for number,event in enumerate(events[:30],1): print(number,event["observedAt"],event["semanticScore"],flush=True)

if __name__=="__main__": main()
