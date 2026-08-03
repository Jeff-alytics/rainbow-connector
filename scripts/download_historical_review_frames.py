#!/usr/bin/env python
"""Download FAA archive frames for the historical review queue and build the
blinded review page.

Reads validation/historical-review-queue/queue.json (stage 2 output). For each
queue event it fetches the FAA image list for the event window, keeps frames
from the event's best bow-facing camera nearest each MRMS scan time, downloads
them with hashes, and finally writes review.html — a self-contained blinded
grading page (no V1/V2 verdicts, scores, or tiers visible; deterministic
shuffled order; labels kept in localStorage with JSON export).

Downloads run oldest-event-first because FAA retention (~30 days) erodes the
early-July days first. Zero frames returned for a whole event window is
recorded as `aged_out`, never treated as a weather negative.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

FAA_API = "https://weathercams.faa.gov/api"
FAA_HEADERS = {
    "Referer": "https://weathercams.faa.gov/",
    "Origin": "https://weathercams.faa.gov",
    "User-Agent": "Mozilla/5.0 rainbow-connector-historical-study",
}
SCHEMA_VERSION = "historical-review-frames.v1"
WINDOW_PAD_MINUTES = 10
MAX_FRAMES_PER_EVENT = 10


def iso_z(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fetch_image_list(site_id: str, start: str, end: str, session: requests.Session) -> list[dict]:
    url = f"{FAA_API}/sites/{site_id}/images"
    for attempt in range(4):
        try:
            response = session.get(url, params={"startTime": start, "endTime": end},
                                   headers=FAA_HEADERS, timeout=30)
            if response.status_code in (429, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            response.raise_for_status()
            payload = response.json()
            images = payload.get("payload") if isinstance(payload, dict) else payload
            return images if isinstance(images, list) else []
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    return []


def image_time(image: dict) -> str | None:
    for key in ("imageDatetime", "capturedAt", "timestamp"):
        if image.get(key):
            return str(image[key])
    return None


def absolute_uri(uri: str) -> str:
    if uri.startswith("http"):
        return uri
    return f"https://images.wcams-static.faa.gov/{uri.lstrip('/')}"


def select_frames(images: list[dict], camera_ids: list[str], scan_times: list[str]) -> tuple[str | None, list[dict]]:
    """Frames from the first listed camera that has any, nearest each scan time."""
    for camera_id in camera_ids:
        frames = []
        for image in images:
            if str(image.get("cameraId")) != str(camera_id) or not image.get("imageUri"):
                continue
            stamp = image_time(image)
            if stamp:
                frames.append({"observedAt": stamp, "uri": absolute_uri(str(image["imageUri"]))})
        if not frames:
            continue
        frames.sort(key=lambda frame: frame["observedAt"])
        chosen: dict[str, dict] = {}
        for scan in scan_times:
            scan_at = parse_utc(scan)
            best = min(frames, key=lambda frame: abs((parse_utc(frame["observedAt"]) - scan_at).total_seconds()))
            if abs((parse_utc(best["observedAt"]) - scan_at).total_seconds()) <= 20 * 60:
                chosen[best["uri"]] = best
        selected = sorted(chosen.values(), key=lambda frame: frame["observedAt"])[:MAX_FRAMES_PER_EVENT]
        if selected:
            return str(camera_id), selected
    return None, []


def download_frame(uri: str, dest: Path, session: requests.Session) -> dict:
    if dest.exists() and dest.stat().st_size > 0:
        payload = dest.read_bytes()
        return {"file": dest.name, "bytes": len(payload), "sha256": sha256_bytes(payload), "cached": True}
    for attempt in range(3):
        try:
            response = session.get(uri, headers=FAA_HEADERS, timeout=45)
            if response.status_code in (429, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            response.raise_for_status()
            dest.write_bytes(response.content)
            return {"file": dest.name, "bytes": len(response.content),
                    "sha256": sha256_bytes(response.content), "cached": False}
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def shuffle_key(event_id: str) -> str:
    return hashlib.sha256(("blind-shuffle:" + event_id).encode()).hexdigest()


def reviewed_event_ids(queue_dir: Path) -> set[str]:
    record = queue_dir / "reviewed-rounds.json"
    if not record.exists():
        return set()
    rounds = json.loads(record.read_text(encoding="utf-8")).get("rounds", [])
    return {event_id for entry in rounds for event_id in entry.get("eventIds", [])}


def build_review_html(events: list[dict], output: Path, exclude: set[str] | None = None) -> None:
    """Self-contained blinded grading page. Blind: no tier, verdicts, or scores.
    Events already shown in a previous review round are left off the page."""
    exclude = exclude or set()
    events = [event for event in events if event["eventId"] not in exclude]
    display = sorted(events, key=lambda event: shuffle_key(event["eventId"]))
    payload = [{
        "eventId": event["eventId"],
        "siteName": event.get("siteName"),
        "cameraId": event.get("downloadedCameraId"),
        "cameraDirection": next((camera.get("direction") for camera in event.get("cameras", [])
                                 if str(camera.get("cameraId")) == str(event.get("downloadedCameraId"))), None),
        "frames": [{"file": f"images/{event['eventId']}/{frame['file']}", "observedAt": frame["observedAt"]}
                   for frame in event.get("downloadedFrames", [])],
    } for event in display if event.get("downloadedFrames")]
    html = """<!doctype html>
<meta charset="utf-8">
<title>Historical FAA review — blinded</title>
<style>
 body{font-family:Segoe UI,system-ui,sans-serif;margin:0;background:#111;color:#ddd}
 header{position:sticky;top:0;background:#1b1b1b;padding:10px 16px;border-bottom:1px solid #333;z-index:2;display:flex;gap:16px;align-items:center}
 header b{color:#fff} button{background:#2d2d2d;color:#ddd;border:1px solid #555;padding:6px 12px;cursor:pointer}
 .event{border-bottom:2px solid #333;padding:14px 16px}
 .event.reviewed{opacity:.45}
 .event h3{margin:0 0 2px;font-size:14px;color:#bbb;font-weight:600;display:flex;gap:12px;align-items:center}
 .event h3 .ev-buttons button{font-size:11px;padding:2px 8px}
 .event h3 .ev-buttons button.on{background:#4a4a4a;border-color:#999;color:#fff}
 .meta{font-size:12px;color:#777;margin-bottom:8px}
 .frames{display:flex;flex-wrap:wrap;gap:10px}
 figure{margin:0;width:340px}
 figure img{width:100%;display:block;background:#000;cursor:zoom-in}
 figcaption{font-size:11px;color:#888;padding:3px 0;display:flex;justify-content:space-between;align-items:center}
 .labels button{font-size:11px;padding:2px 7px;margin-left:3px}
 .labels button.on-rainbow{background:#1d5c2f;border-color:#3f9e5f;color:#fff}
 .labels button.on-no_rainbow{background:#4a4a4a;border-color:#777;color:#fff}
 .labels button.on-unusable{background:#5c1d1d;border-color:#9e3f3f;color:#fff}
 #zoom{position:fixed;inset:0;background:rgba(0,0,0,.94);display:none;align-items:center;justify-content:center;z-index:5}
 #zoom img{max-width:98vw;max-height:98vh}
</style>
<header>
 <b>Blinded historical review</b>
 <span id="progress"></span>
 <button onclick="exportLabels()">Export labels JSON</button>
 <span style="font-size:12px;color:#777">Per event: mark "no rainbow" or label rainbow frames (hover + 1 rainbow / 3 unusable). Unmarked events stay in the next batch.</span>
</header>
<div id="events"></div>
<div id="zoom" onclick="this.style.display='none'"><img></div>
<script>
const EVENTS = __PAYLOAD__;
const KEY = "historical-review-labels-v1";
const EVENT_KEY = "historical-review-event-labels-v1";
const labels = JSON.parse(localStorage.getItem(KEY) || "{}");
const eventLabels = JSON.parse(localStorage.getItem(EVENT_KEY) || "{}");
let hovered = null;
function save(){
  localStorage.setItem(KEY, JSON.stringify(labels));
  localStorage.setItem(EVENT_KEY, JSON.stringify(eventLabels));
}
// In-place updates only: a full re-render resets hover state and scroll, which
// made labeling a second rainbow frame in the same event impossible.
function refreshFrame(file){
  const state = labels[file]?.label;
  document.querySelectorAll('[data-file="' + CSS.escape(file) + '"] .labels button').forEach(control => {
    control.className = control.dataset.value === state ? "on-" + state : "";
  });
}
function refreshEvent(eventId){
  const state = eventLabels[eventId]?.label;
  const section = document.querySelector('[data-event="' + CSS.escape(eventId) + '"]');
  if (!section) return;
  section.classList.toggle("reviewed", !!state);
  section.querySelectorAll(".ev-buttons button").forEach(control => {
    control.className = control.dataset.value === state ? "on" : "";
  });
  const done = EVENTS.filter(event => eventLabels[event.eventId]).length;
  document.getElementById("progress").textContent = done + " / " + EVENTS.length + " events reviewed";
}
function setLabel(file, value){
  labels[file] = { label: value, at: new Date().toISOString() };
  const owner = EVENTS.find(event => event.frames.some(frame => frame.file === file));
  if (value === "rainbow" && owner) eventLabels[owner.eventId] = { label: "rainbow", at: new Date().toISOString() };
  save();
  refreshFrame(file);
  if (owner) refreshEvent(owner.eventId);
}
function setEventLabel(eventId, value){
  eventLabels[eventId] = { label: value, at: new Date().toISOString() };
  save();
  refreshEvent(eventId);
}
function exportLabels(){
  const out = { schemaVersion: "historical-review-labels.v2", exportedAt: new Date().toISOString(),
    convention: "only events present in eventLabels were reviewed; absent events remain unreviewed",
    eventLabels: Object.entries(eventLabels).map(([eventId, entry]) => ({ eventId, ...entry })),
    labels: Object.entries(labels).map(([file, entry]) => ({ file, ...entry })) };
  const blob = new Blob([JSON.stringify(out, null, 2)], { type: "application/json" });
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(blob); anchor.download = "historical-review-labels.json"; anchor.click();
}
function render(){
  const done = EVENTS.filter(event => eventLabels[event.eventId]).length;
  document.getElementById("progress").textContent = done + " / " + EVENTS.length + " events reviewed";
  const host = document.getElementById("events"); host.innerHTML = "";
  for (const event of EVENTS){
    const section = document.createElement("section");
    const eventState = eventLabels[event.eventId]?.label;
    section.className = "event" + (eventState ? " reviewed" : "");
    section.dataset.event = event.eventId;
    const title = document.createElement("h3");
    title.textContent = event.siteName + " — camera " + event.cameraId +
      (event.cameraDirection ? " (facing " + event.cameraDirection + ")" : "");
    const buttons = document.createElement("span"); buttons.className = "ev-buttons";
    for (const value of ["no_rainbow","rainbow","unusable"]){
      const control = document.createElement("button");
      control.textContent = value.replace("_"," ") + (value === "no_rainbow" ? " in event" : "");
      control.dataset.value = value;
      if (eventState === value) control.className = "on";
      control.onclick = () => setEventLabel(event.eventId, value);
      buttons.appendChild(control);
    }
    title.appendChild(buttons);
    section.appendChild(title);
    const meta = document.createElement("div"); meta.className = "meta";
    meta.textContent = event.frames.length + " frames";
    section.appendChild(meta);
    const row = document.createElement("div"); row.className = "frames";
    for (const frame of event.frames){
      const figure = document.createElement("figure");
      figure.dataset.file = frame.file;
      const state = labels[frame.file]?.label;
      const img = document.createElement("img");
      img.loading = "lazy"; img.src = frame.file;
      img.onclick = () => {
        const zoom = document.getElementById("zoom");
        zoom.querySelector("img").src = frame.file; zoom.style.display = "flex";
      };
      const caption = document.createElement("figcaption");
      const stamp = document.createElement("span");
      stamp.textContent = frame.observedAt.replace("T"," ").slice(0, 17) + "Z";
      const controls = document.createElement("span"); controls.className = "labels";
      for (const value of ["rainbow","no_rainbow","unusable"]){
        const control = document.createElement("button");
        control.textContent = value.replace("_"," ");
        control.dataset.value = value;
        if (state === value) control.className = "on-" + value;
        control.onclick = () => setLabel(frame.file, value);
        controls.appendChild(control);
      }
      caption.appendChild(stamp); caption.appendChild(controls);
      figure.appendChild(img); figure.appendChild(caption);
      figure.onmouseenter = () => hovered = frame.file;
      row.appendChild(figure);
    }
    section.appendChild(row); host.appendChild(section);
  }
}
document.addEventListener("keydown", keyEvent => {
  if (!hovered) return;
  if (keyEvent.key === "1") setLabel(hovered, "rainbow");
  if (keyEvent.key === "2") setLabel(hovered, "no_rainbow");
  if (keyEvent.key === "3") setLabel(hovered, "unusable");
});
render();
</script>
"""
    output.write_text(html.replace("__PAYLOAD__", json.dumps(payload)), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-dir", type=Path, default=ROOT / "validation/historical-review-queue")
    parser.add_argument("--limit-events", type=int, default=0)
    parser.add_argument("--tiers", default="go,possible,near_miss")
    args = parser.parse_args()

    queue_dir = args.queue_dir if args.queue_dir.is_absolute() else ROOT / args.queue_dir
    queue: list[dict[str, Any]] = json.loads((queue_dir / "queue.json").read_text(encoding="utf-8"))
    tiers = set(args.tiers.split(","))
    queue = [event for event in queue if event.get("disposition") in tiers]
    # oldest first: FAA retention erodes early days
    download_order = sorted(queue, key=lambda event: event["startAt"])
    if args.limit_events:
        download_order = download_order[:args.limit_events]

    images_root = queue_dir / "images"
    session = requests.Session()
    results = []
    for number, event in enumerate(download_order, 1):
        start = iso_z(parse_utc(event["startAt"]) - timedelta(minutes=WINDOW_PAD_MINUTES))
        end = iso_z(parse_utc(event["endAt"]) + timedelta(minutes=WINDOW_PAD_MINUTES))
        camera_ids = [str(camera["cameraId"]) for camera in event.get("cameras", [])]
        status, frames, camera_used = "ok", [], None
        try:
            images = fetch_image_list(event["siteId"], start, end, session)
            camera_used, picked = select_frames(images, camera_ids, event["scanTimes"])
            if not picked:
                status = "aged_out_or_no_frames"
            else:
                event_dir = images_root / event["eventId"]
                event_dir.mkdir(parents=True, exist_ok=True)
                for frame in picked:
                    name = frame["uri"].rsplit("/", 1)[-1].split("?")[0] or "frame.jpg"
                    meta = download_frame(frame["uri"], event_dir / name, session)
                    frames.append({**meta, "observedAt": frame["observedAt"], "sourceUrl": frame["uri"]})
        except Exception as error:
            status = f"error: {str(error)[:200]}"
        entry = {**event, "downloadStatus": status, "downloadedCameraId": camera_used,
                 "downloadedFrames": frames}
        results.append(entry)
        print(f"[{number}/{len(download_order)}] {event['eventId']} tier={event['disposition']} "
              f"-> {status} frames={len(frames)}", flush=True)
        # save incrementally so a failure keeps everything fetched so far
        (queue_dir / "frames-manifest.json").write_text(json.dumps({
            "schemaVersion": SCHEMA_VERSION, "events": results}, indent=1) + "\n", encoding="utf-8")

    build_review_html(results, queue_dir / "review.html", exclude=reviewed_event_ids(queue_dir))
    got = sum(1 for entry in results if entry["downloadedFrames"])
    print(json.dumps({"events": len(results), "withFrames": got,
                      "frames": sum(len(entry['downloadedFrames']) for entry in results),
                      "reviewPage": str(queue_dir / 'review.html')}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
