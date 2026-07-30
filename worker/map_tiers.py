"""Geometry-only static ingredients-map tiers derived from an MRMS sidecar."""
from __future__ import annotations
import gzip, hashlib, json, math
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from detector_core import solar_position

SCHEMA_VERSION = "map-tiers.v1"
TIER_RULE_VERSION = "map-tiers-2026-07-v1"
MAX_GZIP_BYTES = 150_000

def _decode(sidecar, field, tiered=False):
    g=sidecar["grid"]; out=np.zeros((g["latitudeCount"],g["longitudeCount"]),dtype=np.uint8)
    for row,first,last,value in sidecar.get(field) or []:
        out[int(row),int(first):int(last)+1]=int(value) if tiered else 1
    return out

def _block_fraction(mask):
    rows,cols=mask.shape[0]//4,mask.shape[1]//4
    return mask[:rows*4,:cols*4].reshape(rows,4,cols,4).mean(axis=(1,3))

def _dilate(mask,radius):
    out=np.zeros_like(mask,dtype=bool); rows,cols=mask.shape
    for dr in range(-radius,radius+1):
      for dc in range(-radius,radius+1):
        if dr*dr+dc*dc>radius*radius: continue
        r0,r1=max(0,-dr),min(rows,rows-dr); c0,c1=max(0,-dc),min(cols,cols-dc)
        out[r0+dr:r1+dr,c0+dc:c1+dc]|=mask[r0:r1,c0:c1]
    return out

def _relative(bearing,center): return (bearing-center+180.0)%360.0-180.0

def _centered_arc(occupied):
    occupied=occupied.copy(); old=np.flatnonzero(occupied)
    for left,right in zip(old,old[1:]):
        if 0<right-left-1<=2: occupied[left+1:right]=True
    indices=np.flatnonzero(occupied)
    if not indices.size:return False
    breaks=np.flatnonzero(np.diff(indices)>1); starts=np.r_[0,breaks+1]; ends=np.r_[breaks,indices.size-1]
    return any(int(indices[e]-indices[s]+1)>=30 and int(indices[e])>=35 and int(indices[s])<=64 for s,e in zip(starts,ends))

def _green_ok(lat,lon,anti,sun,tiers,latitudes,longitudes):
    scale=max(.2,math.cos(math.radians(lat)))
    rows=np.flatnonzero(np.abs(latitudes-lat)<=30/111); cols=np.flatnonzero(np.abs(longitudes-lon)<=30/(111*scale))
    if not rows.size or not cols.size:return False
    local=tiers[np.ix_(rows,cols)]; north=(latitudes[rows][:,None]-lat)*111; east=(longitudes[cols][None,:]-lon)*111*scale
    distance=np.hypot(north,east); bearing=(np.degrees(np.arctan2(east,north))+360)%360; rel=_relative(bearing,anti)
    selected=(distance>=8)&(distance<=30)&(np.abs(rel)<=50)&(local>=2); occupied=np.zeros(100,dtype=bool)
    if np.any(selected):occupied[np.unique(np.clip(np.floor(rel[selected]+50).astype(int),0,99))]=True
    if not _centered_arc(occupied):return False
    sunward=(distance>=6)&(distance<=20)&(np.abs(_relative(bearing,sun))<=10)&(local>=2)
    return not bool(np.any(sunward))

def _mask_path():
    packaged=Path(__file__).with_name("assets")/"us-nation.geojson"
    return packaged if packaged.exists() else Path(__file__).parents[1]/"docs"/"mockups"/"us-nation.geojson"

def _geometry(indices,grid,land):
    if not len(indices):return shape({"type":"GeometryCollection","geometries":[]})
    lat0,dlat=float(grid["latitudeStart"]),float(grid["latitudeStepDeg"]); lon0,dlon=float(grid["longitudeStart"]),float(grid["longitudeStepDeg"])
    cells=[]
    for row,col in indices:
        a,b=lat0+row*4*dlat-dlat/2,lat0+(row+1)*4*dlat-dlat/2; c,d=lon0+col*4*dlon-dlon/2,lon0+(col+1)*4*dlon-dlon/2
        cells.append(box(min(c,d),min(a,b),max(c,d),max(a,b)))
    return unary_union(cells).intersection(land)

def _feature(geometry):return {"type":"Feature","properties":{},"geometry":mapping(geometry)}
def _raw(payload):return (json.dumps(payload,separators=(",",":"),sort_keys=True)+"\n").encode()

def build_map_tiers(sidecar):
    if sidecar.get("schemaVersion")!="mrms-rain-footprint.v2":raise ValueError("map tiers require mrms-rain-footprint.v2")
    g=sidecar["grid"]; tiers=_decode(sidecar,"runs",True); display=_decode(sidecar,"displayWetRuns")
    rain_blocks=_block_fraction(display)>=.30; dry=_block_fraction(tiers>=1)<=.05; significant=_block_fraction(tiers>=2)>0
    block_km=abs(float(g["latitudeStepDeg"]))*111*4
    candidates=np.argwhere(_dilate(significant,max(1,math.ceil(30/block_km)))&dry)
    latitudes=float(g["latitudeStart"])+np.arange(g["latitudeCount"])*float(g["latitudeStepDeg"])
    longitudes=float(g["longitudeStart"])+np.arange(g["longitudeCount"])*float(g["longitudeStepDeg"])
    observed=datetime.fromisoformat(sidecar["observedAt"].replace("Z","+00:00")).astimezone(timezone.utc); green=[]
    for row,col in candidates:
        rr,cc=min(int(row*4+2),len(latitudes)-1),min(int(col*4+2),len(longitudes)-1); lat,lon=float(latitudes[rr]),float(longitudes[cc])
        elevation,sun=solar_position(observed,lat,lon)
        if 0<=elevation<=30 and _green_ok(lat,lon,(sun+180)%360,sun,tiers,latitudes,longitudes):green.append((row,col))
    land=shape(json.loads(_mask_path().read_text(encoding="utf-8"))["geometry"]); rain=_geometry(np.argwhere(rain_blocks),g,land); aligned=_geometry(green,g,land).difference(rain)
    now=datetime.now(timezone.utc); generated=now.isoformat().replace("+00:00","Z"); lag=max(0,round((now-observed).total_seconds(),1))
    base={"schemaVersion":SCHEMA_VERSION,"scanTime":sidecar["observedAt"],"generatedAt":generated,"generationLagSeconds":lag,
      "rainFootprintId":sidecar["rainFootprintId"],"tierRuleVersion":TIER_RULE_VERSION,"quietSky":not bool(rain_blocks.any()),
      "thresholdSnapshot":{"blockCells":4,"rainThresholdMmHr":.2,"rainWetFractionMin":.30,"observerWetFractionMax":.05,
        "greenRainThresholdMmHr":1.0,"greenArcMinDeg":30,"greenCenterHalfWidthDeg":15,"greenDistanceKm":[8,30],
        "sunwardClearDistanceKm":[6,20],"sunwardHalfWidthDeg":10,"sunElevationDeg":[0,30]},
      "stats":{"rainBlocks":int(rain_blocks.sum()),"greenBlocks":len(green)}}
    for tolerance in (.012,.02,.03,.05):
        payload={**base,"simplifyToleranceDeg":tolerance,"rain":_feature(rain.simplify(tolerance,preserve_topology=True)),
                 "aligned":_feature(aligned.simplify(tolerance,preserve_topology=True))}
        raw=_raw(payload)
        if len(gzip.compress(raw,mtime=0))<=MAX_GZIP_BYTES:
            payload["contentSha256"]=hashlib.sha256(raw).hexdigest(); return payload
    raise ValueError("map tier payload exceeds 150 KB gzipped")

def encode_payload(payload):
    clean=dict(payload);clean.pop("contentSha256",None);raw=_raw(clean)
    return raw,gzip.compress(raw,mtime=0),hashlib.sha256(raw).hexdigest()
