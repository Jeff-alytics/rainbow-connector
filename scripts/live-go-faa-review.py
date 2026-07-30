#!/usr/bin/env python3
"""Capture FAA WeatherCam evidence near archived Rainbow Connector GO events."""
from __future__ import annotations

import html
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation" / "live-go"
SITES_CACHE = ROOT / "validation" / "faa" / "sites.json"
FAA_API = "https://weathercams.faa.gov/api"
FAA_HEADERS = {"Referer": "https://weathercams.faa.gov/", "Origin": "https://weathercams.faa.gov",
               "User-Agent": "Mozilla/5.0 rainbow-connector-live-validation"}
EVENT_INDEX, EVENT_PREFIX = "rainbow:go:events", "rainbow:go:event:"


def redis(command):
    response = requests.post(os.environ["UPSTASH_REDIS_REST_URL"].strip().rstrip("/"), headers={
        "Authorization": f"Bearer {os.environ['UPSTASH_REDIS_REST_TOKEN'].strip()}", "Content-Type": "application/json"
    }, json=command, timeout=30)
    response.raise_for_status()
    return response.json().get("result")


def load_events(limit=100):
    events = []
    for event_id in redis(["ZREVRANGE", EVENT_INDEX, 0, limit - 1]) or []:
        raw = redis(["GET", EVENT_PREFIX + event_id])
        if raw:
            events.append(json.loads(raw))
    return events


def load_sites():
    if SITES_CACHE.exists():
        return json.loads(SITES_CACHE.read_text(encoding="utf-8"))
    response = requests.get(f"{FAA_API}/sites", headers=FAA_HEADERS, timeout=120)
    response.raise_for_status()
    payload = response.json()["payload"]
    SITES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SITES_CACHE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def distance_km(a_lat, a_lon, b_lat, b_lon):
    rad = math.pi / 180
    dlat, dlon = (b_lat - a_lat) * rad, (b_lon - a_lon) * rad
    value = math.sin(dlat / 2) ** 2 + math.cos(a_lat * rad) * math.cos(b_lat * rad) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(value))


def angle_difference(a, b):
    return abs((a - b + 180) % 360 - 180)


def match_site(event, sites, max_distance=125):
    row = event["representative"]
    bearing = (row.get("direction") or {}).get("bearing")
    choices = []
    for site in sites:
        if not site.get("siteActive") or not site.get("cameras"):
            continue
        distance = distance_km(row["lat"], row["lon"], float(site["latitude"]), float(site["longitude"]))
        if distance > max_distance:
            continue
        cameras = [camera for camera in site["cameras"]
                   if camera.get("cameraBearing") is not None and not camera.get("cameraOutOfOrder")]
        if not cameras:
            continue
        camera = min(cameras, key=lambda item: angle_difference(float(item["cameraBearing"]), float(bearing))) if bearing is not None else cameras[0]
        difference = angle_difference(float(camera["cameraBearing"]), float(bearing)) if bearing is not None else 180
        if bearing is not None and difference > 75:
            continue
        choices.append((distance + difference * .45, distance, difference, site, camera))
    return min(choices, default=None, key=lambda item: item[0])


def capture(event, match):
    _, distance, difference, site, camera = match
    center = datetime.fromisoformat(event["representative"]["detectedAt"].replace("Z", "+00:00"))
    response = requests.get(f"{FAA_API}/sites/{site['siteId']}/images", params={
        "startTime": (center - timedelta(minutes=18)).isoformat().replace("+00:00", "Z"),
        "endTime": (center + timedelta(minutes=18)).isoformat().replace("+00:00", "Z"),
    }, headers=FAA_HEADERS, timeout=60)
    response.raise_for_status()
    images = [image for image in (response.json().get("payload") or [])
              if image.get("cameraId") == camera["cameraId"]]
    images.sort(key=lambda image: abs((datetime.fromisoformat(image["imageDatetime"].replace("Z", "+00:00")) - center).total_seconds()))
    selected = sorted(images[:5], key=lambda image: image["imageDatetime"])
    event_dir = OUT / event["id"]
    event_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for number, image in enumerate(selected, 1):
        path = event_dir / f"{number:02d}.jpg"
        download = requests.get(image["imageUri"], headers=FAA_HEADERS, timeout=60)
        download.raise_for_status()
        path.write_bytes(download.content)
        frames.append({"file": path.relative_to(OUT).as_posix(), "observedAt": image["imageDatetime"],
                       "sourceUrl": image["imageUri"]})
    return {"event": event, "site": site, "camera": camera, "distanceKm": round(distance, 1),
            "bearingDifference": round(difference, 1), "frames": frames}


def build_page(rows, unmatched):
    articles = []
    for number, row in enumerate(rows, 1):
        event, rep, site, camera = row["event"], row["event"]["representative"], row["site"], row["camera"]
        figures = "".join(
            f'<figure><a href="{html.escape(frame["file"])}" target="_blank"><img src="{html.escape(frame["file"])}"></a>'
            f'<figcaption>{html.escape(frame["observedAt"])}</figcaption></figure>' for frame in row["frames"])
        location = rep.get("label") or f'{rep["lat"]:.2f}, {rep["lon"]:.2f}'
        facts = (f'{number}. {event["id"]} · GO score {event.get("peakScore")} · scans {event.get("scanCount")} · '
                 f'{location} · {rep["detectedAt"]}<br>FAA {site["siteName"]}, {site.get("state", "")} · '
                 f'{row["distanceKm"]} km away · {camera.get("cameraDirection", "camera")} '
                 f'({camera.get("cameraBearing")}°) · {row["bearingDifference"]}° from predicted bow bearing')
        articles.append(f'<article><h2>{facts}</h2><div class="strip">{figures}</div></article>')
    page = OUT / "review.html"
    page.write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>Live GO review</title><style>body{background:#101114;color:#eee;font:16px system-ui;margin:0}'
        'main{max-width:1500px;margin:auto;padding:24px}article{background:#1b1d22;padding:16px;margin:0 0 28px}'
        '.strip{display:flex;gap:10px;overflow-x:auto}figure{flex:0 0 520px;margin:0}img{width:520px;height:340px;'
        'object-fit:contain;background:#000}figcaption{color:#bbb;padding:5px}h2{font-size:17px;line-height:1.5}</style>'
        f'</head><body><main><h1>Live Rainbow Connector GO review</h1><p>{len(rows)} events with aligned FAA imagery; '
        f'{len(unmatched)} archived events had no suitable FAA camera within 125 km.</p>'
        + "".join(articles) + "</main></body></html>", encoding="utf-8")
    (OUT / "review.json").write_text(json.dumps({"generatedAt": datetime.now(timezone.utc).isoformat(),
        "matched": rows, "unmatchedEventIds": unmatched}, indent=2) + "\n", encoding="utf-8")
    return page


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    events, sites = load_events(), load_sites()
    rows, unmatched = [], []
    for event in events:
        match = match_site(event, sites)
        if not match:
            unmatched.append(event["id"])
            continue
        try:
            row = capture(event, match)
            (rows if row["frames"] else unmatched).append(row if row["frames"] else event["id"])
        except (requests.RequestException, KeyError, ValueError):
            unmatched.append(event["id"])
    rows.sort(key=lambda row: (-len(row["frames"]), -float(row["event"].get("peakScore") or 0)))
    page = build_page(rows, unmatched)
    print(json.dumps({"events": len(events), "matched": len(rows), "unmatched": len(unmatched), "review": str(page)}))


if __name__ == "__main__":
    main()
