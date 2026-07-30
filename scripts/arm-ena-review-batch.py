#!/usr/bin/env python3
"""Download and render a full-resolution review batch for ranked ARM ENA events."""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(r"C:\Users\jeffm\rainbow-finder")
OUT = ROOT / "validation" / "arm-ena"
REVIEW = OUT / "review"
ASI = "enaasiskyimageC1.a1"
DOWNLOAD = "https://adc.arm.gov/armlive/saveData"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scanner = load_module("ena_scan", ROOT / "scripts" / "arm-ena-sunshower-scan.py")
reviewer = load_module("movie_review", ROOT / "scripts" / "arm-movie-review.py")


def image_time(name):
    match = re.search(r"(20\d{6})[._T-]?(\d{6})", Path(name).name)
    if not match:
        return None
    return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def download_archive(info, auth):
    directory = OUT / "downloads"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / info["file_name"]
    if path.exists() and path.stat().st_size == info["file_size"]:
        return path
    temporary = path.with_suffix(path.suffix + ".part")
    with requests.get(DOWNLOAD, params={"user": auth, "file": info["file_name"]},
                      headers={"User-Agent": "rainbow-connector-validation/1.0"},
                      stream=True, timeout=180) as response:
        response.raise_for_status()
        with temporary.open("wb") as output:
            for chunk in response.iter_content(262144):
                output.write(chunk)
    temporary.replace(path)
    return path


def selected_members(archive, center):
    with tarfile.open(archive, "r") as bundle:
        available = [(member, image_time(member.name)) for member in bundle.getmembers()
                     if member.isfile() and member.name.lower().endswith((".jpg", ".jpeg"))]
        available = [(member, stamp) for member, stamp in available if stamp]
        chosen = []
        for offset in (-600, -300, 0, 300, 600):
            target = center.timestamp() + offset
            member, stamp = min(available, key=lambda item: abs(item[1].timestamp() - target))
            chosen.append((member, stamp))
        frame_dir = OUT / "frames" / center.strftime("%Y%m%dT%H%M%SZ")
        frame_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for member, stamp in chosen:
            destination = frame_dir / Path(member.name).name
            if not destination.exists():
                source = bundle.extractfile(member)
                destination.write_bytes(source.read())
            paths.append((destination, stamp))
        return paths


def render_candidate(number, event, archive):
    center = datetime.fromisoformat(event["observedAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
    frames = selected_members(archive, center)
    sheet = Image.new("RGB", (1800, 1500))
    for index, (path, _) in enumerate(frames):
        sheet.paste(reviewer.panorama(path), (0, index * 300))
    candidate_id = f"ena-{number:02d}"
    output = REVIEW / f"{candidate_id}-fullres-360.jpg"
    sheet.save(output, quality=92, optimize=True)
    return {"candidateId": candidate_id, "image": output.name, **event}


def write_page(items, batch):
    articles = []
    for item in items:
        facts = (f'{item["matchingMinutes"]} matching rain+sun minutes · '
                 f'DNI {item["measuredDirectNormalIrradianceWm2"]:.0f} W/m² · '
                 f'rain rate {item["measuredRainRateMmHr"]:.2f} mm/hr · '
                 f'sun elevation {item["sunElevationDeg"]:.1f}° · '
                 f'expected bow top {item["expectedRainbowTopElevationDeg"]:.1f}°')
        articles.append(f'''<article>
<h2>{html.escape(item["candidateId"])} — {html.escape(item["observedAt"])}</h2>
<p class="facts">{html.escape(facts)}</p>
<img loading="lazy" src="{html.escape(item["image"])}" alt="{html.escape(item["candidateId"])} full-resolution sequence">
</article>''')
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARM ENA rainbow review batch {batch}</title><style>
body{{margin:0;background:#111;color:#eee;font:16px/1.45 system-ui,sans-serif}}main{{max-width:1500px;margin:auto;padding:24px}}
article{{margin:0 0 48px;padding:20px;background:#1d1d1d;border-radius:10px}}h1,h2{{margin-top:0}}p{{color:#ccc}}.facts{{color:#9fd}}
img{{display:block;width:100%;height:auto;background:#000}}
</style></head><body><main><h1>Azores possibilities — batch {batch}</h1>
<p>Five full-resolution 360° horizon panoramas per event: −10, −5, 0, +5, and +10 minutes. Label each rainbow, no rainbow, or uncertain.</p>
{''.join(articles)}</main></body></html>'''
    output = REVIEW / f"ena-review-batch-{batch:02d}.html"
    output.write_text(page, encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    ranked = json.loads((OUT / "surface-sunshower-ranked.json").read_text(encoding="utf-8"))["events"]
    start = (args.batch - 1) * args.count
    events = ranked[start:start + args.count]
    asi = scanner.file_index(ASI, 2000)
    missing = [event["day"] for event in events if event["day"] not in asi]
    if missing:
        raise SystemExit(f"No ASI archive for: {', '.join(missing)}")
    auth = scanner.credentials()
    unique = {event["day"]: asi[event["day"]] for event in events}
    archives = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(download_archive, info, auth): day for day, info in unique.items()}
        for number, future in enumerate(as_completed(futures), 1):
            day = futures[future]
            archives[day] = future.result()
            print(f"Downloaded {number}/{len(futures)}: {day}", flush=True)
    REVIEW.mkdir(parents=True, exist_ok=True)
    rendered = []
    for number, event in enumerate(events, start + 1):
        rendered.append(render_candidate(number, event, archives[event["day"]]))
        print(f"Rendered {number - start}/{len(events)}: ena-{number:02d}", flush=True)
    output = write_page(rendered, args.batch)
    manifest = REVIEW / f"ena-review-batch-{args.batch:02d}.json"
    manifest.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
