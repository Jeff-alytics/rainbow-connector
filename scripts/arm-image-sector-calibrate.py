#!/usr/bin/env python3
"""Calibrate an anti-solar-sector rainbow score from labeled ARM ENA imagery."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
FRAMES = ROOT / "validation" / "arm-ena" / "frames"
LABELS = ROOT / "validation" / "arm-lamont" / "human-labels.json"
OUTPUT = ROOT / "validation" / "arm-ena" / "image-sector-calibration.json"
LAT, LON = 39.0916, -28.0257


def image_time(path):
    match = re.search(r"(20\d{6})[._-]?(\d{6})", path.name)
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def solar(date):
    rad = math.pi / 180
    days = date.timestamp() / 86400 - 0.5 + 2440588 - 2451545
    anomaly = rad * (357.5291 + 0.98560028 * days)
    longitude = anomaly + rad * (1.9148*math.sin(anomaly)+.02*math.sin(2*anomaly)+.0003*math.sin(3*anomaly)) + rad*102.9372 + math.pi
    declination = math.asin(math.sin(rad*23.4397)*math.sin(longitude))
    ra = math.atan2(math.sin(longitude)*math.cos(rad*23.4397), math.cos(longitude))
    ha = rad*(280.16+360.9856235*days) + LON*rad - ra
    latitude = LAT*rad
    elevation = math.asin(math.sin(latitude)*math.sin(declination)+math.cos(latitude)*math.cos(declination)*math.cos(ha))/rad
    azimuth = math.atan2(math.sin(ha), math.cos(ha)*math.sin(latitude)-math.tan(declination)*math.cos(latitude))/rad + 180
    return elevation, azimuth % 360


def box_mean(array, radius=6):
    padded = np.pad(array, radius, mode="edge")
    integral = np.pad(padded, ((1,0),(1,0))).cumsum(0).cumsum(1)
    size = radius*2+1
    return (integral[size:,size:] - integral[:-size,size:] - integral[size:,:-size] + integral[:-size,:-size]) / (size*size)


def detected_sun_angle(path):
    im = Image.open(path).convert("RGB").resize((512,512), Image.Resampling.LANCZOS)
    rgb = np.asarray(im,dtype=float)/255
    hsv = np.asarray(im.convert("HSV"),dtype=float)/255
    elev, bearing = solar(image_time(path))
    yy,xx=np.mgrid[:512,:512]; dx=xx-255.5; dy=yy-255.5; r=np.sqrt(dx*dx+dy*dy)
    expected=246*(90-elev)/90
    mask=(abs(r-expected)<35)&(r<248)
    bright=box_mean(rgb.mean(2)*(1-.35*hsv[...,1]))
    bright[~mask]=-1
    y,x=np.unravel_index(np.argmax(bright),bright.shape)
    return math.degrees(math.atan2(y-255.5,x-255.5))%360,bearing,elev


def circular_difference(a,b):
    return (a-b+180)%360-180


def orientation(paths):
    detections=[detected_sun_angle(p) for p in paths if solar(image_time(p))[0]>5]
    best=None
    for sign in (1,-1):
        offsets=np.radians([(angle-sign*bearing)%360 for angle,bearing,_ in detections])
        offset=math.degrees(math.atan2(np.sin(offsets).mean(),np.cos(offsets).mean()))%360
        residuals=[abs(circular_difference(angle,(sign*bearing+offset)%360)) for angle,bearing,_ in detections]
        score=float(np.median(residuals))
        if best is None or score<best[0]: best=(score,sign,offset,residuals)
    return best


def sector_score(path, sign, offset):
    im=Image.open(path).convert("RGB").resize((512,512),Image.Resampling.LANCZOS)
    rgb=np.asarray(im,dtype=float)/255; hsv=np.asarray(im.convert("HSV"),dtype=float)/255
    hue,sat,val=hsv[...,0],hsv[...,1],hsv[...,2]
    yy,xx=np.mgrid[:512,:512]; dx=xx-255.5; dy=yy-255.5
    radius=np.sqrt(dx*dx+dy*dy); angle=(np.degrees(np.arctan2(dy,dx))+360)%360
    elev,bearing=solar(image_time(path)); anti=(sign*((bearing+180)%360)+offset)%360
    top=max(0,42-elev); top_radius=246*(90-top)/90
    sector=(radius>=max(70,top_radius-35))&(radius<=246)&(abs((angle-anti+180)%360-180)<=58)
    scores=[]
    for size in (24,40,64):
        step=size//2
        for y in range(0,513-size,step):
            for x in range(0,513-size,step):
                mask=sector[y:y+size,x:x+size]&(sat[y:y+size,x:x+size]>.2)&(val[y:y+size,x:x+size]>.18)
                if mask.sum()<size*size*.1: continue
                h=hue[y:y+size,x:x+size][mask]; s=sat[y:y+size,x:x+size][mask]
                groups=np.array([np.mean((h<.08)|(h>.94)),np.mean((h>=.08)&(h<.18)),np.mean((h>=.18)&(h<.42)),np.mean((h>=.42)&(h<.66)),np.mean((h>=.66)&(h<.82)),np.mean((h>=.82)&(h<=.94))])
                active=np.sum(groups>.025); warm=groups[0]+groups[1]; green=groups[2]; violet=groups[4]+groups[5]
                diversity=-float(np.sum(groups[groups>0]*np.log(groups[groups>0])))
                coexist=min(warm,green+violet); nonblue=warm+green+violet
                scores.append(nonblue*(1+active/3)*(.5+diversity)*(.5+float(s.mean()))*(.5+coexist*4))
    return round(max(scores,default=0),5)


def main():
    labels=json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    labeled=[]; allpaths=[]
    for label in labels:
        if not re.match(r"ena-\d+-fullres",label["candidateId"]) or label["label"] not in ("rainbow","no_rainbow"): continue
        directory=FRAMES/label["centerTime"].replace("-","").replace(":","")
        paths=sorted(directory.glob("*.jpg")) if directory.exists() else []
        if paths: labeled.append((label,paths)); allpaths.extend(paths)
    residual,sign,offset,residuals=orientation(allpaths)
    print("orientation",sign,offset,"medianResidual",residual,flush=True)
    rows=[]
    for label,paths in labeled:
        scores=[sector_score(path,sign,offset) for path in paths]
        score=max(scores)
        rows.append({"candidateId":label["candidateId"],"label":label["label"],"score":score,"frameScores":scores})
        print(label["candidateId"],label["label"],score,flush=True)
    rows.sort(key=lambda row:row["score"],reverse=True)
    OUTPUT.write_text(json.dumps({"schemaVersion":1,"orientation":{"sign":sign,"offsetDeg":offset,"medianResidualDeg":residual},"rows":rows},indent=2)+"\n",encoding="utf-8")
    for kind in ("rainbow","no_rainbow"):
        values=[row["score"] for row in rows if row["label"]==kind]
        print(kind,len(values),min(values),sum(values)/len(values),max(values))
    print(OUTPUT)


if __name__=="__main__": main()
