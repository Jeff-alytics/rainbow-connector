#!/usr/bin/env python3
"""Image-first anti-solar scan of cached full-day ARM ENA ASI archives."""
from __future__ import annotations

import io
import json
import math
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

ROOT=Path(r"C:\Users\jeffm\rainbow-finder")
ARCHIVES=ROOT/"validation"/"arm-ena"/"downloads"
LABELS=ROOT/"validation"/"arm-lamont"/"human-labels.json"
OUTPUT=ROOT/"validation"/"arm-ena"/"image-first-cached-ranked.json"
LAT,LON=39.0916,-28.0257
SIGN,OFFSET=-1,254.97494434003312


def timestamp(name):
    m=re.search(r"(20\d{6})[._-]?(\d{6})",Path(name).name)
    return datetime.strptime("".join(m.groups()),"%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) if m else None


def solar(date):
    rad=math.pi/180; days=date.timestamp()/86400-.5+2440588-2451545
    anomaly=rad*(357.5291+.98560028*days)
    longitude=anomaly+rad*(1.9148*math.sin(anomaly)+.02*math.sin(2*anomaly)+.0003*math.sin(3*anomaly))+rad*102.9372+math.pi
    dec=math.asin(math.sin(rad*23.4397)*math.sin(longitude)); ra=math.atan2(math.sin(longitude)*math.cos(rad*23.4397),math.cos(longitude))
    ha=rad*(280.16+360.9856235*days)+LON*rad-ra; lat=LAT*rad
    elev=math.asin(math.sin(lat)*math.sin(dec)+math.cos(lat)*math.cos(dec)*math.cos(ha))/rad
    az=math.atan2(math.sin(ha),math.cos(ha)*math.sin(lat)-math.tan(dec)*math.cos(lat))/rad+180
    return elev,az%360


def grid_score(image,date,size):
    image=image.convert("RGB").resize((256,256),Image.Resampling.LANCZOS)
    hsv=np.asarray(image.convert("HSV"),dtype=float)/255
    h,s,v=hsv[...,0],hsv[...,1],hsv[...,2]
    yy,xx=np.mgrid[:256,:256]; dx=xx-127.5; dy=yy-127.5
    radius=np.sqrt(dx*dx+dy*dy); angle=(np.degrees(np.arctan2(dy,dx))+360)%360
    elev,bearing=solar(date); anti=(SIGN*((bearing+180)%360)+OFFSET)%360
    top=max(0,42-elev); top_radius=123*(90-top)/90
    sector=(radius>=max(35,top_radius-18))&(radius<=123)&(abs((angle-anti+180)%360-180)<=58)
    valid=sector&(s>.2)&(v>.18)
    categories=[valid&((h<.08)|(h>.94)),valid&(h>=.08)&(h<.18),valid&(h>=.18)&(h<.42),valid&(h>=.42)&(h<.66),valid&(h>=.66)&(h<.82),valid&(h>=.82)&(h<=.94)]
    n=256//size
    def sums(a): return a.reshape(n,size,n,size).sum(axis=(1,3))
    counts=sums(valid); denom=np.maximum(counts,1)
    groups=np.stack([sums(a)/denom for a in categories],axis=-1)
    active=(groups>.025).sum(-1); warm=groups[...,0]+groups[...,1]; green=groups[...,2]; violet=groups[...,4]+groups[...,5]
    nonblue=warm+green+violet; coexist=np.minimum(warm,green+violet)
    diversity=-(groups*np.log(np.maximum(groups,1e-9))).sum(-1)
    mean_sat=sums(s*valid)/denom
    score=nonblue*(1+active/3)*(.5+diversity)*(.5+mean_sat)*(.5+coexist*4)
    score[counts<size*size*.08]=0
    return float(score.max())


def score(image,date): return round(max(grid_score(image,date,16),grid_score(image,date,32)),5)


def main():
    labels=json.loads(LABELS.read_text(encoding="utf-8"))["labels"]
    reviewed=[datetime.fromisoformat(item["centerTime"].replace("Z","+00:00")) for item in labels if item["candidateId"].startswith("ena-")]
    frames=[]; archives=sorted(ARCHIVES.glob("enaasiskyimageC1.a1.*.jpg.tar"))
    for number,archive in enumerate(archives,1):
        with tarfile.open(archive,"r") as bundle:
            members=[]
            for member in bundle.getmembers():
                date=timestamp(member.name)
                if not member.isfile() or not date or date.minute%2 or date.second: continue
                elev,_=solar(date)
                if 0<elev<25 and all(abs((date-known).total_seconds())>1200 for known in reviewed): members.append((member,date,elev))
            for member,date,elev in members:
                try:
                    image=Image.open(io.BytesIO(bundle.extractfile(member).read()))
                    frames.append({"observedAt":date.isoformat().replace("+00:00","Z"),"archive":archive.name,"member":member.name,
                                   "sunElevationDeg":round(elev,2),"expectedRainbowTopElevationDeg":round(42-elev,2),"visualScore":score(image,date)})
                except Exception: pass
        print(f"Scanned {number}/{len(archives)} archives; {len(frames)} frames",flush=True)
    frames.sort(key=lambda item:item["observedAt"])
    groups=[]
    for frame in frames:
        date=datetime.fromisoformat(frame["observedAt"].replace("Z","+00:00"))
        if groups and (date-datetime.fromisoformat(groups[-1][-1]["observedAt"].replace("Z","+00:00"))).total_seconds()<=600:
            groups[-1].append(frame)
        else: groups.append([frame])
    events=[]
    for group in groups:
        best=max(group,key=lambda item:item["visualScore"])
        events.append({**best,"eventStart":group[0]["observedAt"],"eventEnd":group[-1]["observedAt"],"screenedFrames":len(group)})
    events.sort(key=lambda item:item["visualScore"],reverse=True)
    OUTPUT.write_text(json.dumps({"schemaVersion":1,"archives":len(archives),"frames":len(frames),"events":events},indent=2)+"\n",encoding="utf-8")
    print(OUTPUT)
    for i,event in enumerate(events[:30],1): print(i,event["observedAt"],event["visualScore"],flush=True)


if __name__=="__main__": main()
