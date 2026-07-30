#!/usr/bin/env python3
"""Weather-first, radar-refined WebCOOS archive miner."""
import argparse, hashlib, importlib.util, json, math, os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/"validation"/"webcoos"; CACHE=OUT/"cache"
API="https://app.webcoos.org/webcoos/api/v1"; METEO="https://archive-api.open-meteo.com/v1/archive"
UA="RainbowConnectorArchiveResearch/1.0"; RAD=math.pi/180

def adiff(a,b): return abs((a-b+540)%360-180)
def bearing(a,o,b,p):
    a,b=a*RAD,b*RAD; d=(p-o)*RAD
    return (math.degrees(math.atan2(math.sin(d)*math.cos(b),math.cos(a)*math.sin(b)-math.sin(a)*math.cos(b)*math.cos(d)))+360)%360
def dest(lat,lon,direction,km):
    q=km/6371.0088; a=lat*RAD; o=lon*RAD; t=direction*RAD
    b=math.asin(math.sin(a)*math.cos(q)+math.cos(a)*math.sin(q)*math.cos(t))
    p=o+math.atan2(math.sin(t)*math.sin(q)*math.cos(a),math.cos(q)-math.sin(a)*math.sin(b))
    return math.degrees(b),math.degrees(p)
def wedge(raw,lat,lon):
    values=[]
    for p in ((raw or {}).get("coordinates") or [[]])[0]:
        if len(p)>=2 and math.hypot((p[1]-lat)*111,(p[0]-lon)*85)>=.2: values.append(bearing(lat,lon,p[1],p[0]))
    if not values:return None,None
    center=(math.degrees(math.atan2(sum(math.sin(x*RAD) for x in values),sum(math.cos(x*RAD) for x in values)))+360)%360
    return round(center,1),round(min(90,max(adiff(x,center) for x in values)),1)
def solar(when,lat,lon):
    d=when.timetuple().tm_yday; h=when.hour+when.minute/60
    g=2*math.pi/365*(d-1+(h-12)/24)
    decl=.006918-.399912*math.cos(g)+.070257*math.sin(g)-.006758*math.cos(2*g)+.000907*math.sin(2*g)-.002697*math.cos(3*g)+.00148*math.sin(3*g)
    eq=229.18*(.000075+.001868*math.cos(g)-.032077*math.sin(g)-.014615*math.cos(2*g)-.040849*math.sin(2*g))
    ha=(h*60+eq+4*lon)/4-180; x=ha*RAD; p=lat*RAD
    elev=90-math.degrees(math.acos(max(-1,min(1,math.sin(p)*math.sin(decl)+math.cos(p)*math.cos(decl)*math.cos(x)))))
    az=(math.degrees(math.atan2(math.sin(x),math.cos(x)*math.sin(p)-math.tan(decl)*math.cos(p)))+180)%360
    return elev,(az+180)%360
def fetch(url,params,headers=None,prefix="api"):
    CACHE.mkdir(parents=True,exist_ok=True); key=hashlib.sha256((url+json.dumps(params,sort_keys=True)).encode()).hexdigest()[:24]; path=CACHE/f"{prefix}-{key}.json"
    if path.exists():return json.loads(path.read_text(encoding="utf-8"))
    error=None
    for attempt in range(3):
        try:
            r=requests.get(url,params=params,headers={"User-Agent":UA,**(headers or {})},timeout=90)
            r.raise_for_status();data=r.json();path.write_text(json.dumps(data),encoding="utf-8");time.sleep(.25);return data
        except requests.RequestException as exc:
            error=exc
            if attempt<2:time.sleep(2**attempt*3)
    raise error
def catalog(token):
    h={"Authorization":f"Token {token}","Accept":"application/json"}; data=fetch(f"{API}/assets/",{"page_size":100},h,"catalog"); out=[]
    for asset in data.get("results",[]):
        if (asset.get("disposition") or {}).get("slug")!="up":continue
        props=((asset.get("data") or {}).get("properties") or {}); loc=((props.get("location") or {}).get("coordinates") or [])
        if len(loc)<2:continue
        lat,lon=float(loc[1]),float(loc[0])
        if not(24<=lat<=50 and -125<=lon<=-66):continue
        services=[]
        for feed in asset.get("feeds",[]):
            for product in feed.get("products",[]):
                if (((product.get("data") or {}).get("common") or {}).get("slug")=="one-minute-stills"):services+=product.get("services",[])
        services.sort(key=lambda x:(x.get("elements") or {}).get("last_starting") or "",reverse=True)
        if not services:continue
        service=services[0]; common=((service.get("data") or {}).get("common") or {}); ext=service.get("elements") or {}; center,half=wedge(props.get("wedge"),lat,lon)
        if not common.get("slug") or center is None:continue
        ac=(asset.get("data") or {}).get("common") or {}
        out.append(dict(id=ac.get("slug"),name=ac.get("label"),state=props.get("state_or_territory"),lat=lat,lon=lon,service=common["slug"],first=ext.get("first_starting"),last=ext.get("last_starting"),count=int(ext.get("count") or 0),cameraBearing=center,halfFovDeg=half))
    return out
def points(cam):
    out=[dict(lat=cam["lat"],lon=cam["lon"],distanceKm=0,bearing=None)]
    for km in (15,25):
        for d in range(0,360,45):
            lat,lon=dest(cam["lat"],cam["lon"],d,km);out.append(dict(lat=lat,lon=lon,distanceKm=km,bearing=d))
    return out
def meteo(pts,start,end,kind,variables):
    params=dict(latitude=",".join(f"{p['lat']:.4f}" for p in pts),longitude=",".join(f"{p['lon']:.4f}" for p in pts),start_date=start,end_date=end,timezone="GMT");params[kind]=variables
    data=fetch(METEO,params,prefix=f"meteo-{kind}");return data if isinstance(data,list) else [data]
def top_days(cam,start,end,limit):
    rows=meteo(points(cam),start,end,"daily","precipitation_sum,sunshine_duration"); days=[]
    for i,day in enumerate(rows[0]["daily"]["time"]):
        sun=float(rows[0]["daily"]["sunshine_duration"][i] or 0); rain=max(float(x["daily"]["precipitation_sum"][i] or 0) for x in rows[1:])
        if sun>=900 and rain>=.5:days.append((math.log1p(rain)*math.log1p(sun/60),day))
    return [x[1] for x in sorted(days,reverse=True)[:limit]]
def hourly(cam,days):
    pts=points(cam); found=[]
    for day in days:
        rows=meteo(pts,day,day,"hourly","precipitation,direct_normal_irradiance_instant,cloud_cover");best=None
        for i,stamp in enumerate(rows[0]["hourly"]["time"]):
            observed=datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc);elev,bow=solar(observed,cam["lat"],cam["lon"]);error=max(0,adiff(cam["cameraBearing"],bow)-cam["halfFovDeg"]);dni=float(rows[0]["hourly"]["direct_normal_irradiance_instant"][i] or 0)
            if not(5<=elev<=22) or error>12 or dni<120:continue
            rain=[(float(row["hourly"]["precipitation"][i] or 0),p) for p,row in zip(pts[1:],rows[1:]) if adiff(p["bearing"],bow)<=32 and float(row["hourly"]["precipitation"][i] or 0)>=.05]
            if not rain:continue
            amount,p=max(rain,key=lambda x:x[0]); local=float(rows[0]["hourly"]["precipitation"][i] or 0);cloud=float(rows[0]["hourly"]["cloud_cover"][i] or 0)
            score=25*math.exp(-((elev-13)/7)**2)+25*min(1,max(0,(dni-100)/400))+25*min(1,math.log1p(amount*8)/math.log(9))+(15 if local<.05 else max(0,8-local*8))+10*max(0,1-abs(cloud-55)/55)
            item=dict(cameraId=cam["id"],cameraName=cam["name"],state=cam["state"],service=cam["service"],lat=cam["lat"],lon=cam["lon"],observedAt=observed.isoformat().replace("+00:00","Z"),sunElevationDeg=round(elev,2),bowBearing=round(bow,1),rainbowArcDeg=round(42-elev,2),directionError=round(error,1),directNormalIrradianceWm2=round(dni),cloudCoverPct=round(cloud),observerPrecipitationMm=round(local,3),ringPrecipitationMm=round(amount,3),rainPoint=dict(lat=round(p["lat"],4),lon=round(p["lon"],4),distanceKm=p["distanceKm"],bearing=p["bearing"]),weatherScore=round(score,2))
            if best is None or item["weatherScore"]>best["weatherScore"]:best=item
        if best:found.append(best)
    return found
def radar_module():
    spec=importlib.util.spec_from_file_location("archive_radar",ROOT/"scripts"/"arm-radar-prefilter.py");mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
def radar_download(mod,observed,item,bow):
    error=None
    for attempt in range(3):
        try:
            return mod.download_and_sample(
                observed.isoformat().replace("+00:00","Z"),
                dict(lat=item["lat"],lon=item["lon"]),bow,
            )
        except Exception as exc:
            error=exc
            if attempt<2:time.sleep(2**attempt*3)
    raise error

def refine(item,cam,mod):
    center=datetime.fromisoformat(item["observedAt"].replace("Z","+00:00"));best=None
    for minute in range(-20,21,10):
        observed=center+timedelta(minutes=minute);elev,bow=solar(observed,item["lat"],item["lon"]);error=max(0,adiff(cam["cameraBearing"],bow)-cam["halfFovDeg"])
        if not(5<=elev<=22) or error>12:continue
        stamp=observed.strftime("%Y%m%d%H%M");path=CACHE/f"radar-{item['cameraId']}-{stamp}-{round(bow)}.json"
        try:
            radar=json.loads(path.read_text()) if path.exists() else radar_download(mod,observed,item,bow)
            if not path.exists():path.write_text(json.dumps(radar))
            bins=[radar["byDistanceKm"][str(x)] for x in mod.DISTANCES_KM if x<=22];near=max((x["maxDbz"] for x in bins if x["maxDbz"] is not None),default=-32);wet=sum(x["wetPoints"] for x in bins);observer=radar.get("observerDbz")
            if near<10 or wet<1 or(observer is not None and observer>=25):continue
            rs=mod.rank_score(dict(directNormalIrradianceWm2=item["directNormalIrradianceWm2"]),radar);score=.55*item["weatherScore"]+.45*min(100,max(0,(rs+10)*1.5))
            row={**item,"observedAt":observed.isoformat().replace("+00:00","Z"),"sunElevationDeg":round(elev,2),"rainbowArcDeg":round(42-elev,2),"bowBearing":round(bow,1),"directionError":round(error,1),"radarScore":rs,"score":round(score,1),"rainIntensity":round(min(1,max(0,near)/35),3),"observerRainIntensity":round(min(1,max(0,observer or 0)/35),3),"radar":dict(observedAt=radar.get("radarAt"),observerDbz=observer,antiSolarMaxDbz=near,antiSolarWetPoints=wet)}
            if best is None or row["score"]>best["score"]:best=row
        except Exception as e: print(f"  radar miss {stamp}: {e}",flush=True)
    return best
def frame_count(item,token):
    center=datetime.fromisoformat(item["observedAt"].replace("Z","+00:00"));params=dict(starting_after=(center-timedelta(minutes=7)).isoformat().replace("+00:00","Z"),starting_before=(center+timedelta(minutes=7)).isoformat().replace("+00:00","Z"),service=item["service"],page_size=20)
    data=fetch(f"{API}/elements/",params,{"Authorization":f"Token {token}","Accept":"application/json"},"elements")
    return sum(int((((x.get("data") or {}).get("properties") or {}).get("size") or 0)>=12000) for x in data.get("results",[]))
def seed(items,url,secret):
    out=[]
    for i,item in enumerate(items,1):
        print(f"Seeding {i}/{len(items)} {item['cameraName']}",flush=True);r=requests.post(url,json={"candidate":item},headers={"Authorization":f"Bearer {secret}","User-Agent":UA},timeout=75)
        try:body=r.json()
        except Exception:body={"error":r.text[:300]}
        out.append(dict(cameraId=item["cameraId"],observedAt=item["observedAt"],status=r.status_code,response=body))
    return out
def main():
    p=argparse.ArgumentParser();p.add_argument("--start",default="2024-01-01");p.add_argument("--end",default=(datetime.now(timezone.utc)-timedelta(days=3)).date().isoformat());p.add_argument("--camera-limit",type=int,default=20);p.add_argument("--days-per-camera",type=int,default=10);p.add_argument("--radar-shortlist",type=int,default=24);p.add_argument("--limit",type=int,default=10);p.add_argument("--seed-url");a=p.parse_args()
    token=os.environ.get("WEBCOOS_API_TOKEN","").strip()
    if not token:raise SystemExit("WEBCOOS_API_TOKEN is required")
    cams=sorted(catalog(token),key=lambda x:x["count"],reverse=True)[:a.camera_limit];weather=[];errors=[];print(f"Scanning {len(cams)} cameras",flush=True)
    for i,cam in enumerate(cams,1):
        start=max(a.start,str(cam.get("first") or a.start)[:10]);end=min(a.end,str(cam.get("last") or a.end)[:10]);print(f"[{i}/{len(cams)}] {cam['name']}",flush=True)
        try:
            days=top_days(cam,start,end,a.days_per_camera);hits=hourly(cam,days);weather+=hits;print(f"  {len(days)} days; {len(hits)} matches",flush=True)
        except Exception as e:errors.append(dict(cameraId=cam["id"],error=str(e)));print(f"  error: {e}",flush=True)
    weather.sort(key=lambda x:x["weatherScore"],reverse=True);short=[];counts={}
    for x in weather:
        if counts.get(x["cameraId"],0)>=2:continue
        counts[x["cameraId"]]=counts.get(x["cameraId"],0)+1;short.append(x)
        if len(short)>=a.radar_shortlist:break
    mod=radar_module();refined=[];by_id={x["id"]:x for x in cams}
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs={pool.submit(refine,x,by_id[x["cameraId"]],mod):x for x in short}
        for i,future in enumerate(as_completed(jobs),1):
            x=jobs[future];print(f"Radar {i}/{len(short)} finished {x['cameraName']}",flush=True)
            try: hit=future.result()
            except Exception as e:
                print(f"  radar error: {e}",flush=True);continue
            if hit:
                try: available=frame_count(hit,token)
                except Exception as e:
                    print(f"  frame check failed: {e}",flush=True);continue
                print(f"  radar qualified {hit['score']}; archived frames {available}",flush=True)
                if available>=5:
                    hit["availableFrames"]=available
                    refined.append(hit);print(f"  accepted {hit['score']}",flush=True)
    refined.sort(key=lambda x:x["score"],reverse=True);selected=refined[:a.limit]
    for i,x in enumerate(selected,1):x["rank"]=i
    payload=dict(schemaVersion=1,generatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),purpose="Historical discovery only; human grades are separate from live GO alerts.",window=dict(start=a.start,end=a.end),method=dict(weather="Open-Meteo hourly reanalysis",radar="IEM NEXRAD N0Q anti-solar fan",direction="WebCOOS published viewshed"),camerasScanned=len(cams),weatherCandidates=len(weather),weatherShortlist=short,radarCandidates=len(refined),errors=errors,candidates=selected)
    OUT.mkdir(parents=True,exist_ok=True);manifest=OUT/"archive-candidates.json";manifest.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8");print(f"Wrote {manifest} with {len(selected)} candidates",flush=True)
    if a.seed_url:
        secret=os.environ.get("ALERT_NOTIFY_SECRET","").strip()
        if not secret:raise SystemExit("ALERT_NOTIFY_SECRET is required with --seed-url")
        payload["seedResults"]=seed(selected,a.seed_url,secret);manifest.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__":main()
