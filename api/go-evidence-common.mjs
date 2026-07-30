import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { put } from "@vercel/blob";
import { distKm } from "./alert-common.mjs";
import { attachGoEventEvidence, loadRecentGoEvents } from "./go-event-common.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const FAA_API = "https://weathercams.faa.gov/api";
const CHART_FEED = "https://chartexp1.sha.maryland.gov/CHARTExportClientService/getCameraMapDataJSON.do";
const CHART_THUMBNAILS = "https://chart.maryland.gov/wwwroot/thumbnails";
const FAA_HEADERS = {
  Referer: "https://weathercams.faa.gov/",
  Origin: "https://weathercams.faa.gov",
  "User-Agent": "Mozilla/5.0 rainbow-connector-review",
};
let siteCache = null;
let chartCache = null;
let chartCacheAt = 0;

async function sites() {
  if (!siteCache) siteCache = JSON.parse(await readFile(path.join(ROOT, "faa-sites-compact.json"), "utf8"));
  return siteCache;
}

async function chartSites() {
  if (chartCache && Date.now() - chartCacheAt < 60 * 60 * 1000) return chartCache;
  const response = await fetch(CHART_FEED, { headers: { "User-Agent": FAA_HEADERS["User-Agent"] } });
  if (!response.ok) throw new Error(`Maryland CHART camera list failed (${response.status})`);
  const payload = await response.json();
  chartCache = (payload.data || []).filter(camera =>
    Number.isFinite(camera.lat) && Number.isFinite(camera.lon)
    && camera.commMode === "ONLINE"
    && !["COMM_FAILURE", "HARDWARE_FAILURE"].includes(camera.opStatus));
  chartCacheAt = Date.now();
  return chartCache;
}

function angleDifference(a, b) {
  return Math.abs((a - b + 540) % 360 - 180);
}

export function matchFaaCamera(event, catalog, maxDistanceKm = 35, maxBearingDifference = 50) {
  const rep = event?.representative || {};
  const bearing = Number(rep.direction?.bearing);
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon) || !Number.isFinite(bearing)) return null;
  let best = null;
  for (const site of catalog || []) {
    const distanceKm = distKm(rep.lat, rep.lon, site.lat, site.lon);
    if (distanceKm > maxDistanceKm) continue;
    for (const camera of site.cameras || []) {
      const bearingDifference = angleDifference(camera.bearing, bearing);
      if (bearingDifference > maxBearingDifference) continue;
      const score = distanceKm + bearingDifference * 0.35;
      if (!best || score < best.score) best = { site, camera, distanceKm, bearingDifference, score };
    }
  }
  return best;
}

export function matchChartCameras(event, catalog, maxDistanceKm = 35, limit = 5) {
  const rep = event?.representative || {};
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon)) return [];
  return (catalog || [])
    .map(camera => ({ camera, distanceKm: distKm(rep.lat, rep.lon, camera.lat, camera.lon) }))
    .filter(match => match.distanceKm <= maxDistanceKm)
    .sort((a, b) => a.distanceKm - b.distanceKm)
    .slice(0, Math.max(1, Math.min(Number(limit) || 5, 5)));
}

function iso(value) {
  return new Date(value).toISOString();
}

async function faaFrames(event, match) {
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const response = await fetch(`${FAA_API}/sites/${match.site.id}/images?${new URLSearchParams({
    startTime: iso(centerMs - 25 * 60 * 1000),
    endTime: iso(Math.min(Date.now(), centerMs + 25 * 60 * 1000)),
  })}`, { headers: FAA_HEADERS });
  if (!response.ok) throw new Error(`FAA image list failed (${response.status})`);
  const payload = await response.json();
  return (payload.payload || [])
    .filter(image => String(image.cameraId) === String(match.camera.id) && image.imageUri)
    .sort((a, b) => Math.abs(new Date(a.imageDatetime) - centerMs) - Math.abs(new Date(b.imageDatetime) - centerMs))
    .slice(0, 5)
    .sort((a, b) => new Date(a.imageDatetime) - new Date(b.imageDatetime));
}

async function storeFrame(event, match, frame) {
  const response = await fetch(frame.imageUri, { headers: FAA_HEADERS });
  if (!response.ok) throw new Error(`FAA image failed (${response.status})`);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 8000) throw new Error("FAA image was too small to review.");
  const stamp = String(frame.imageDatetime || Date.now()).replace(/[^0-9]/g, "").slice(0, 17);
  const stored = await put(`go-evidence/${event.id}/${match.camera.id}-${stamp}.jpg`, bytes, {
    access: "public",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: "image/jpeg",
  });
  return { url: stored.url, observedAt: frame.imageDatetime || null };
}

async function storeChartFrame(event, match) {
  const observedAt = new Date().toISOString();
  const imageUrl = `${CHART_THUMBNAILS}/${match.camera.id}.jpg?t=${Date.now()}`;
  const response = await fetch(imageUrl, { headers: { "User-Agent": FAA_HEADERS["User-Agent"] }, cache: "no-store" });
  if (!response.ok) throw new Error(`Maryland CHART image failed (${response.status})`);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 8000) throw new Error("Maryland CHART image was too small to review.");
  const stamp = observedAt.replace(/[^0-9]/g, "").slice(0, 17);
  const stored = await put(`go-evidence/${event.id}/chart-${match.camera.id}-${stamp}.jpg`, bytes, {
    access: "public",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: "image/jpeg",
  });
  return {
    url: stored.url,
    observedAt,
    source: "Maryland CHART",
    cameraName: match.camera.description || match.camera.name || match.camera.id,
    distanceKm: Number(match.distanceKm.toFixed(1)),
  };
}

async function captureChartEvidence(event) {
  const matches = matchChartCameras(event, await chartSites());
  if (!matches.length) return { stored: false, reason: "no_chart_match", eventId: event.id };
  const storedFrames = [];
  for (const match of matches) {
    try { storedFrames.push(await storeChartFrame(event, match)); } catch {}
  }
  if (!storedFrames.length) return { stored: false, reason: "no_usable_chart_frames", eventId: event.id };
  await attachGoEventEvidence(event.id, {
    status: "ready",
    source: "Maryland CHART",
    camera: {
      name: "Maryland CHART nearby views",
      state: "MD",
      direction: "Multiple views; bearing not published",
      distanceKm: storedFrames[0].distanceKm,
      bearingDifference: null,
    },
    frames: storedFrames,
  });
  return { stored: true, eventId: event.id, frames: storedFrames.length, source: "Maryland CHART" };
}

export async function captureFaaEvidence(event) {
  if ((event?.evidence?.frames || []).length >= 3) return { stored: false, reason: "already_captured", eventId: event.id };
  const match = matchFaaCamera(event, await sites());
  if (!match) {
    const chart = await captureChartEvidence(event);
    if (chart.stored) return chart;
    await attachGoEventEvidence(event.id, { status: "no_camera_match", source: "FAA WeatherCam + Maryland CHART" });
    return { stored: false, reason: "no_camera_match", eventId: event.id };
  }
  const frames = await faaFrames(event, match);
  if (frames.length < 3) return { stored: false, reason: "not_enough_frames_yet", eventId: event.id, frames: frames.length };
  const storedFrames = [];
  for (const frame of frames) {
    try { storedFrames.push(await storeFrame(event, match, frame)); } catch {}
  }
  if (storedFrames.length < 3) return { stored: false, reason: "not_enough_usable_frames", eventId: event.id, frames: storedFrames.length };
  await attachGoEventEvidence(event.id, {
    status: "ready",
    source: "FAA WeatherCam",
    camera: {
      siteId: match.site.id,
      cameraId: match.camera.id,
      name: match.site.name,
      state: match.site.state,
      direction: match.camera.direction,
      distanceKm: Number(match.distanceKm.toFixed(1)),
      bearingDifference: Number(match.bearingDifference.toFixed(1)),
    },
    frames: storedFrames,
  });
  return { stored: true, eventId: event.id, frames: storedFrames.length };
}

export async function capturePendingFaaEvidence(limit = 2) {
  const now = Date.now();
  const pending = (await loadRecentGoEvents(now - 2 * 60 * 60 * 1000, 50))
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => (event.evidence?.frames || []).length < 3)
    .filter(event => !["no_faa_match", "no_camera_match"].includes(event.evidence?.status))
    .filter(event => {
      const age = now - new Date(event.lastSeenAt || event.firstSeenAt).getTime();
      return age >= 0 && age <= 2 * 60 * 60 * 1000;
    })
    .sort((a, b) => Number(b.peakScore || 0) - Number(a.peakScore || 0))
    .slice(0, Math.max(0, Math.min(Number(limit) || 2, 4)));
  const results = [];
  for (const event of pending) {
    try { results.push(await captureFaaEvidence(event)); }
    catch (error) { results.push({ stored: false, eventId: event.id, reason: error.message || "capture_failed" }); }
  }
  return results;
}
