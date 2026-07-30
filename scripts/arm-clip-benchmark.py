#!/usr/bin/env python3
"""Benchmark pretrained CLIP semantics on held-out human-labeled ARM rainbow events."""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from transformers import AutoProcessor, CLIPModel

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
FRAMES=ROOT/"validation"/"arm-ena"/"frames"
OUTPUT=ROOT/"validation"/"arm-ena"/"clip-benchmark.json"
MODEL="openai/clip-vit-base-patch32"


def module():
    path=ROOT/"scripts"/"arm-movie-review.py"
    spec=importlib.util.spec_from_file_location("review",path)
    result=importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    result.LAT,result.LON=39.0916,-28.0257
    result.MAX_ELEVATION_DEG=45.0
    return result


def calibrated_panel(review, path):
    spec=importlib.util.spec_from_file_location("cached_scan",ROOT/"scripts"/"arm-image-cached-scan.py")
    cached=importlib.util.module_from_spec(spec); spec.loader.exec_module(cached)
    date=review.image_time(path); _,bearing=cached.solar(date)
    anti_angle=(cached.SIGN*((bearing+180)%360)+cached.OFFSET)%360
    return review.dewarp(Image.open(path).convert("RGB"),np.radians(anti_angle))


def labels():
    result=[]
    for path in (ROOT/"validation"/"arm-lamont"/"human-labels.json",ROOT/"validation"/"arm-ena"/"image-first-human-labels.json"):
        data=json.loads(path.read_text(encoding="utf-8"))
        for item in data["labels"]:
            if re.match(r"(?:ena-\d+-fullres|visual-\d+)",item["candidateId"]) and item["label"] in ("rainbow","no_rainbow"):
                result.append(item)
    return result


def embedding(value):
    """Unwrap feature tensors across Transformers CLIP API versions."""
    if isinstance(value, torch.Tensor):
        return value
    if getattr(value, "pooler_output", None) is not None:
        return value.pooler_output
    if getattr(value, "image_embeds", None) is not None:
        return value.image_embeds
    if getattr(value, "text_embeds", None) is not None:
        return value.text_embeds
    raise TypeError(f"Unsupported CLIP feature output: {type(value)!r}")


def normalize(value):
    tensor=embedding(value)
    return tensor/tensor.norm(dim=-1,keepdim=True)


def main():
    review=module(); events=[]; images=[]
    for label in labels():
        directory=FRAMES/label["centerTime"].replace("-","").replace(":","")
        paths=sorted(directory.glob("*.jpg")) if directory.exists() else []
        if not paths: continue
        paths=[paths[len(paths)//2]]
        start=len(images); images.extend(calibrated_panel(review,path) for path in paths)
        events.append({"candidateId":label["candidateId"],"label":label["label"],"start":start,"end":len(images)})
    processor=AutoProcessor.from_pretrained(MODEL)
    model=CLIPModel.from_pretrained(MODEL).eval()
    vectors=[]
    with torch.inference_mode():
        for start in range(0,len(images),16):
            inputs=processor(images=images[start:start+16],return_tensors="pt")
            vectors.append(normalize(model.get_image_features(**inputs)).cpu())
        prompts=["a photograph of a natural atmospheric rainbow in the sky","a cloudy sky without a rainbow","sun glare or lens flare in a sky camera","a colorful camera lens artifact"]
        text=processor(text=prompts,return_tensors="pt",padding=True)
        text_vectors=normalize(model.get_text_features(**text)).cpu()
    vectors=torch.cat(vectors)
    similarities=vectors@text_vectors.T
    for event in events:
        segment=similarities[event["start"]:event["end"]]
        event["zeroShotScore"]=round(float((segment[:,0]-segment[:,1:].max(1).values).max()),6)
    # Leave-one-event-out prototype score, preventing each event from teaching its own label.
    event_vectors=[normalize(vectors[event["start"]:event["end"]].mean(0,keepdim=True))[0] for event in events]
    for index,event in enumerate(events):
        positive=torch.stack([v for j,v in enumerate(event_vectors) if j!=index and events[j]["label"]=="rainbow"]).mean(0)
        negative=torch.stack([v for j,v in enumerate(event_vectors) if j!=index and events[j]["label"]=="no_rainbow"]).mean(0)
        positive=positive/positive.norm(); negative=negative/negative.norm()
        frame_vectors=vectors[event["start"]:event["end"]]
        event["prototypeScore"]=round(float((frame_vectors@positive-frame_vectors@negative).max()),6)
    ranked=sorted(events,key=lambda item:item["prototypeScore"],reverse=True)
    positives=sum(item["label"]=="rainbow" for item in events)
    metrics=[]
    for count in (5,10,20):
        selected=ranked[:min(count,len(ranked))]; hits=sum(item["label"]=="rainbow" for item in selected)
        metrics.append({"top":count,"selected":len(selected),"rainbows":hits,"precision":round(hits/len(selected),3),"recall":round(hits/positives,3)})
    OUTPUT.write_text(json.dumps({"schemaVersion":1,"model":MODEL,"events":events,"rankedIds":[item["candidateId"] for item in ranked],"metrics":metrics},indent=2)+"\n",encoding="utf-8")
    print(json.dumps(metrics,indent=2))
    for number,item in enumerate(ranked[:25],1): print(number,item["candidateId"],item["label"],item["prototypeScore"],item["zeroShotScore"])
    print(OUTPUT)


if __name__=="__main__": main()
