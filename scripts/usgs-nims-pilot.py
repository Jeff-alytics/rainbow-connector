"""Screen high-cadence USGS NIMS cameras for rainbow-review sky coverage."""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import requests
from PIL import Image

API = "https://api.waterdata.usgs.gov/nims/v0"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation" / "usgs-nims-pilot"
IMAGES = OUT / "images"
HEADERS = {"User-Agent": "RainbowConnector/1.0 (camera-source feasibility pilot)"}
CONUS_STATES = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
    "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


def get_json(session: requests.Session, url: str, params=None, retries=4):
    for attempt in range(retries):
        response = session.get(url, params=params, headers=HEADERS, timeout=45)
        if response.status_code == 429 and attempt + 1 < retries:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Request failed: {url}")


def target_window(camera):
    try:
        zone = ZoneInfo(camera.get("tz") or "UTC")
    except Exception:
        zone = timezone.utc
    newest = datetime.fromisoformat(camera["newestImageDT"].replace("Z", "+00:00")).astimezone(zone)
    target = (newest - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    return target.astimezone(timezone.utc) - timedelta(minutes=75), target.astimezone(timezone.utc) + timedelta(minutes=75)


def sky_metrics(path: Path):
    image = cv2.imread(str(path))
    if image is None or image.shape[0] < 100 or image.shape[1] < 100:
        return None
    height, width = image.shape[:2]
    scale = 480 / max(height, width)
    small = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))))
    h, w = small.shape[:2]
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB).astype(np.float32)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.float32)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 140) > 0
    local_edges = cv2.blur(edges.astype(np.float32), (13, 13))
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    value, saturation = hsv[..., 2], hsv[..., 1] / 255
    blue = (b > r * 1.04) & (b > g * 0.98) & (value > 65)
    bright_cloud = (saturation < 0.24) & (value > 105) & (local_edges < 0.13)
    dark_cloud = (saturation < 0.18) & (value > 58) & (local_edges < 0.075)
    sky_like = blue | bright_cloud | dark_cloud
    upper = sky_like[: round(h * 0.62)]
    horizon = sky_like[round(h * 0.22): round(h * 0.68)]
    top = sky_like[: max(1, round(h * 0.12))]
    upper_edges = edges[: round(h * 0.62)]
    upper_fraction = float(upper.mean())
    horizon_fraction = float(horizon.mean())
    top_fraction = float(top.mean())
    edge_fraction = float(upper_edges.mean())
    score = 100 * (0.38 * upper_fraction + 0.38 * horizon_fraction + 0.24 * top_fraction)
    score *= max(0.35, 1 - max(0, edge_fraction - 0.12) * 1.8)
    return {
        "width": width,
        "height": height,
        "skyScore": round(score, 1),
        "upperSkyPct": round(100 * upper_fraction, 1),
        "horizonSkyPct": round(100 * horizon_fraction, 1),
        "upperEdgePct": round(100 * edge_fraction, 1),
    }


def fetch_camera(camera):
    with requests.Session() as session:
        after, before = target_window(camera)
        files = get_json(session, f"{API}/listFiles", {
            "camId": camera["camId"],
            "after": after.isoformat().replace("+00:00", "Z"),
            "before": before.isoformat().replace("+00:00", "Z"),
            "recent": "true",
            "rawItem": "true",
            "limit": 1,
        })
        if not files:
            files = get_json(session, f"{API}/listFiles", {
                "camId": camera["camId"], "recent": "true", "rawItem": "true", "limit": 1,
            })
        if not files:
            return None
        item = files[0]
        url = camera["smallDir"] + item["filename"]
        response = session.get(url, headers=HEADERS, timeout=60)
        response.raise_for_status()
        safe_id = "".join(char if char.isalnum() or char in "-_" else "_" for char in camera["camId"])
        path = IMAGES / f"{safe_id}.jpg"
        path.write_bytes(response.content)
        metrics = sky_metrics(path)
        if not metrics:
            path.unlink(missing_ok=True)
            return None
        return {
            "id": camera["camId"],
            "name": camera.get("camName") or camera["camId"],
            "description": camera.get("camDesc"),
            "state": camera.get("stateAbrv"),
            "lat": float(camera["lat"]),
            "lon": float(camera["lng"]),
            "intervalMinutes": int(camera["ingest"]["intr"]),
            "observedAt": item.get("timestamp"),
            "image": f"images/{path.name}",
            "sourceUrl": url,
            **metrics,
        }


def build_page(rows):
    cards = []
    for index, row in enumerate(rows, 1):
        cards.append(f'''<article data-id="{row['id']}">
<div class="rank">#{index}</div><img src="{row['image']}" loading="lazy" alt="USGS camera {row['name']}">
<div class="body"><h2>{row['name']}, {row['state']}</h2>
<p>{row['intervalMinutes']}-minute camera · sky score {row['skyScore']} · horizon sky {row['horizonSkyPct']}%<br>
{row['lat']:.4f}, {row['lon']:.4f} · image {row['observedAt']}</p>
<div class="grade"><button data-grade="usable">Usable sky</button><button data-grade="limited">Limited</button><button data-grade="unusable">Unusable</button></div></div></article>''')
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>USGS NIMS camera pilot</title><style>
body{{margin:0;background:#07111e;color:#eef5fb;font:16px system-ui}}main{{max-width:1500px;margin:auto;padding:28px}}
h1{{margin-bottom:6px}}.intro{{color:#b6c7d8;max-width:920px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:22px}}
article{{position:relative;background:#122237;border:1px solid #29435e;border-radius:12px;overflow:hidden}}img{{width:100%;height:330px;object-fit:contain;background:#02070d}}
.rank{{position:absolute;top:9px;left:9px;background:#07111edb;padding:5px 9px;border-radius:7px;font-weight:700}}.body{{padding:14px}}h2{{font-size:18px;margin:0 0 8px}}p{{margin:0 0 12px;color:#b6c7d8;line-height:1.45}}
.grade{{display:flex;gap:8px}}button{{border:1px solid #54718e;background:#1b344e;color:white;border-radius:7px;padding:9px 12px;cursor:pointer}}button.chosen{{background:#f97316;border-color:#f97316}}
</style></head><body><main><h1>USGS NIMS high-cadence camera pilot</h1><p class="intro">Top {len(rows)} automatically ranked five- and 15-minute cameras. Grade whether the fixed view contains enough open sky near the horizon to be useful for rainbow review. This is camera suitability—not whether the image contains a rainbow.</p>
<div class="grid">{''.join(cards)}</div></main><script>
const key='usgs-nims-camera-grades-v1', grades=JSON.parse(localStorage.getItem(key)||'{{}}');
function paint(){{document.querySelectorAll('article').forEach(a=>a.querySelectorAll('button').forEach(b=>b.classList.toggle('chosen',grades[a.dataset.id]===b.dataset.grade)))}}
document.addEventListener('click',e=>{{if(!e.target.dataset.grade)return;const a=e.target.closest('article');grades[a.dataset.id]=e.target.dataset.grade;localStorage.setItem(key,JSON.stringify(grades));paint()}});paint();
</script></body></html>'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        catalog = get_json(session, f"{API}/cameras")
    now = datetime.now(timezone.utc)
    eligible = []
    for camera in catalog:
        try:
            newest = datetime.fromisoformat(camera["newestImageDT"].replace("Z", "+00:00"))
            interval = int(camera.get("ingest", {}).get("intr"))
            if (not camera.get("hideCam") and camera.get("stateAbrv") in CONUS_STATES
                    and interval <= 15 and now - newest <= timedelta(hours=24)
                    and camera.get("smallDir")):
                eligible.append(camera)
        except (KeyError, TypeError, ValueError):
            continue
    rows = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_camera, camera) for camera in eligible]
        for index, future in enumerate(as_completed(futures), 1):
            try:
                row = future.result()
                if row:
                    rows.append(row)
            except Exception as error:
                print(f"camera failed: {error}")
            if index % 25 == 0:
                print(f"processed {index}/{len(futures)}", flush=True)
    rows.sort(key=lambda row: (row["skyScore"], row["horizonSkyPct"], -row["intervalMinutes"]), reverse=True)
    (OUT / "camera-screen.json").write_text(json.dumps({
        "generatedAt": now.isoformat(), "eligible": len(eligible), "downloaded": len(rows), "cameras": rows,
    }, indent=2), encoding="utf-8")
    review_rows = rows[:60]
    (OUT / "index.html").write_text(build_page(review_rows), encoding="utf-8")
    print(json.dumps({
        "eligible": len(eligible), "downloaded": len(rows), "review": len(review_rows),
        "page": str((OUT / "index.html").resolve()),
        "states": len({row["state"] for row in rows}),
    }))


if __name__ == "__main__":
    main()
