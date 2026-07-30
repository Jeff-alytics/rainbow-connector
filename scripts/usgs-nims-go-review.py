"""Match usable USGS NIMS cameras to recent Rainbow Connector GO history."""

from __future__ import annotations

import html
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "validation" / "usgs-nims-pilot"
OUT = ROOT / "validation" / "usgs-nims-go-review"
API = "https://api.waterdata.usgs.gov/nims/v0"
HISTORY = "https://therainbowconnector.com/api/candidate-history"
HEADERS = {"User-Agent": "RainbowConnector/1.0 (USGS NIMS validation)"}


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def nims_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=timezone.utc)


def load_matches():
    grades = json.loads((PILOT / "grades.json").read_text(encoding="utf-8"))
    screen = json.loads((PILOT / "camera-screen.json").read_text(encoding="utf-8"))
    cameras = [camera for camera in screen["cameras"] if grades.get(camera["id"]) == "usable"]
    matches = []
    with requests.Session() as session:
        for camera in cameras:
            response = session.get(HISTORY, params={
                "lat": camera["lat"], "lon": camera["lon"], "radiusKm": 35,
                "hours": 168, "limit": 2016,
            }, headers=HEADERS, timeout=30)
            response.raise_for_status()
            for match in response.json().get("matches", []):
                matches.append({"camera": camera, **match})
    return matches


def group_events(matches):
    groups = []
    for row in sorted(matches, key=lambda item: item["generatedAt"]):
        stamp = datetime.fromisoformat(row["generatedAt"].replace("Z", "+00:00"))
        existing = next((group for group in groups
                         if group["camera"]["id"] == row["camera"]["id"]
                         and abs((stamp - group["center"]).total_seconds()) <= 30 * 60), None)
        if existing:
            existing["matches"].append(row)
            weights = [datetime.fromisoformat(item["generatedAt"].replace("Z", "+00:00")).timestamp()
                       for item in existing["matches"]]
            existing["center"] = datetime.fromtimestamp(sum(weights) / len(weights), timezone.utc)
        else:
            groups.append({"camera": row["camera"], "center": stamp, "matches": [row]})
    return groups


def capture(group, number):
    camera, center = group["camera"], group["center"]
    response = requests.get(f"{API}/listFiles", params={
        "camId": camera["id"], "after": iso(center - timedelta(minutes=38)),
        "before": iso(center + timedelta(minutes=38)), "rawItem": "true", "limit": 30,
    }, headers=HEADERS, timeout=45)
    response.raise_for_status()
    files = response.json()
    files.sort(key=lambda item: abs((nims_time(item["timestamp"]) - center).total_seconds()))
    selected = sorted(files[:5], key=lambda item: item["timestamp"])
    event_dir = OUT / f"candidate-{number:02d}"
    event_dir.mkdir(parents=True, exist_ok=True)
    base = camera["sourceUrl"].rsplit("/", 1)[0] + "/"
    frames = []
    for index, item in enumerate(selected, 1):
        source = base + item["filename"]
        image = requests.get(source, headers=HEADERS, timeout=60)
        image.raise_for_status()
        path = event_dir / f"{index:02d}.jpg"
        path.write_bytes(image.content)
        frames.append({
            "file": path.relative_to(OUT).as_posix(), "observedAt": item["timestamp"], "sourceUrl": source,
        })
    strongest = max(group["matches"], key=lambda item: float(item["candidate"].get("evidence", {}).get("score") or 0))
    return {
        "camera": camera, "center": iso(center), "matches": group["matches"], "strongest": strongest,
        "frames": frames,
    }


def build_page(rows):
    articles = []
    for number, row in enumerate(rows, 1):
        camera, candidate = row["camera"], row["strongest"]["candidate"]
        evidence = candidate.get("evidence") or {}
        figures = "".join(
            f'<figure><a href="{html.escape(frame["file"])}" target="_blank"><img src="{html.escape(frame["file"])}"></a>'
            f'<figcaption>{html.escape(frame["observedAt"])}</figcaption></figure>' for frame in row["frames"])
        facts = (f'#{number} · {html.escape(camera["name"])} · {camera["state"]} · '
                 f'{row["strongest"]["distanceKm"]:.1f} km from model observer · '
                 f'{len(row["matches"])} GO scan(s) · score {float(evidence.get("score") or 0):.1f}<br>'
                 f'Sun {float(evidence.get("sunElevationDeg") or 0):.1f}° · '
                 f'DNI {float(evidence.get("directNormalIrradianceWm2") or 0):.0f} W/m² · '
                 f'rain signal {float(evidence.get("rainIntensity") or 0):.2f} · '
                 f'predicted bow bearing {candidate.get("direction", {}).get("bearing", "unknown")}° · '
                 f'camera bearing unknown')
        articles.append(f'<article><h2>{facts}</h2><div class="strip">{figures}</div></article>')
    page = OUT / "index.html"
    page.write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>USGS NIMS GO review</title><style>body{background:#07111e;color:#eef5fb;font:16px system-ui;margin:0}'
        'main{max-width:1550px;margin:auto;padding:26px}article{background:#122237;border:1px solid #29435e;padding:16px;margin:0 0 28px}'
        '.strip{display:flex;gap:10px;overflow-x:auto}figure{flex:0 0 660px;margin:0}img{width:660px;height:440px;object-fit:contain;background:#02070d}'
        'figcaption{color:#b6c7d8;padding:5px}h2{font-size:17px;line-height:1.55}</style></head><body><main>'
        f'<h1>USGS NIMS · recent Rainbow Connector GO matches</h1><p>{len(rows)} distinct events. Five archived frames nearest each model event.</p>'
        + "".join(articles) + "</main></body></html>", encoding="utf-8")
    return page


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    groups = group_events(load_matches())
    rows = [capture(group, index) for index, group in enumerate(groups, 1)]
    page = build_page(rows)
    (OUT / "review.json").write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(), "events": rows,
    }, indent=2), encoding="utf-8")
    print(json.dumps({"rawMatches": sum(len(row["matches"]) for row in rows), "events": len(rows), "page": str(page.resolve())}))


if __name__ == "__main__":
    main()
