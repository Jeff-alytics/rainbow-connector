import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { put } from "@vercel/blob";
import { distKm } from "./alert-common.mjs";
import { attachGoEventEvidence, loadRecentGoEvents } from "./go-event-common.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const NIMS_API = "https://api.waterdata.usgs.gov/nims/v0";
const HEADERS = { "User-Agent": "RainbowConnector/1.0 (USGS NIMS review evidence)" };
let whitelistCache = null;

export async function loadNimsWhitelist() {
  if (!whitelistCache) {
    whitelistCache = JSON.parse(await readFile(path.join(ROOT, "usgs-nims-sites.json"), "utf8"));
  }
  return whitelistCache;
}

export function matchNimsCamera(event, cameras, maxDistanceKm = 35) {
  const rep = event?.representative || {};
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon)) return null;
  return (cameras || []).map(camera => ({
    camera,
    distanceKm: distKm(rep.lat, rep.lon, camera.lat, camera.lon),
  })).filter(match => match.distanceKm <= maxDistanceKm)
    .sort((a, b) => (a.camera.viewQuality === "usable" ? 0 : 20) + a.distanceKm
      - ((b.camera.viewQuality === "usable" ? 0 : 20) + b.distanceKm))[0] || null;
}

export function parseNimsTimestamp(value) {
  const match = String(value || "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z$/);
  return match ? `${match[1]}T${match[2]}:${match[3]}:${match[4]}Z` : null;
}

export function rankNimsFrames(files, centerMs, limit = 5) {
  return (files || []).map(file => ({
    ...file,
    observedAt: parseNimsTimestamp(file.timestamp),
  })).filter(file => file.filename && file.observedAt && Number.isFinite(new Date(file.observedAt).getTime()))
    .sort((a, b) => Math.abs(new Date(a.observedAt).getTime() - centerMs)
      - Math.abs(new Date(b.observedAt).getTime() - centerMs))
    .slice(0, limit)
    .sort((a, b) => new Date(a.observedAt) - new Date(b.observedAt));
}

async function nimsFrames(event, match) {
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const intervalMs = Math.max(5, Number(match.camera.intervalMinutes) || 15) * 60 * 1000;
  const windowMs = intervalMs * 2.6;
  const response = await fetch(`${NIMS_API}/listFiles?${new URLSearchParams({
    camId: match.camera.id,
    after: new Date(centerMs - windowMs).toISOString(),
    before: new Date(Math.min(Date.now(), centerMs + windowMs)).toISOString(),
    rawItem: "true",
    limit: "30",
  })}`, { headers: HEADERS });
  if (!response.ok) throw new Error(`USGS NIMS image list failed (${response.status})`);
  return rankNimsFrames(await response.json(), centerMs, 5);
}

async function storeNimsFrame(event, match, frame) {
  const imageUrl = match.camera.imageBaseUrl + frame.filename;
  const response = await fetch(imageUrl, { headers: HEADERS });
  if (!response.ok) throw new Error(`USGS NIMS image failed (${response.status})`);
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 12_000 || bytes.byteLength > 5_000_000) throw new Error("USGS NIMS image size was unsuitable.");
  const observedMs = new Date(frame.observedAt).getTime();
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const timeOffsetMinutes = Number(((observedMs - centerMs) / 60_000).toFixed(1));
  const stamp = frame.observedAt.replace(/[^0-9]/g, "").slice(0, 17);
  const stored = await put(`go-evidence/${event.id}/usgs-nims-${match.camera.id}-${stamp}.jpg`, bytes, {
    access: "public", addRandomSuffix: false, allowOverwrite: true, contentType: "image/jpeg",
  });
  return {
    url: stored.url,
    observedAt: frame.observedAt,
    source: "USGS NIMS",
    cameraName: match.camera.name,
    cameraPageUrl: match.camera.pageUrl,
    distanceKm: Number(match.distanceKm.toFixed(1)),
    bearingDifference: null,
    direction: "Fixed view; bearing not published",
    state: match.camera.state,
    viewQuality: match.camera.viewQuality,
    skyScore: match.camera.skyScore,
    horizonSkyPct: match.camera.horizonSkyPct,
    timeOffsetMinutes,
  };
}

export async function captureNimsEvidence(event) {
  if ((event?.evidence?.frames || []).length) return { stored: false, reason: "already_captured", eventId: event.id };
  const match = matchNimsCamera(event, await loadNimsWhitelist());
  if (!match) return { stored: false, reason: "no_nims_match", eventId: event.id };
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const intervalMinutes = Math.max(5, Number(match.camera.intervalMinutes) || 15);
  if (Date.now() - centerMs < intervalMinutes * 60_000) {
    await attachGoEventEvidence(event.id, {
      status: "waiting_usgs",
      source: "USGS NIMS",
      camera: {
        id: match.camera.id,
        name: match.camera.name,
        state: match.camera.state,
        distanceKm: Number(match.distanceKm.toFixed(1)),
        bearingDifference: null,
        direction: "Fixed view; bearing not published",
        viewQuality: match.camera.viewQuality,
        skyScore: match.camera.skyScore,
        horizonSkyPct: match.camera.horizonSkyPct,
        intervalMinutes,
      },
    });
    return { stored: false, reason: "waiting_for_post_event_frame", eventId: event.id };
  }
  const frames = await nimsFrames(event, match);
  if (frames.length < 3) return { stored: false, reason: "not_enough_nims_frames", eventId: event.id, frames: frames.length };
  const storedFrames = (await Promise.allSettled(
    frames.map(frame => storeNimsFrame(event, match, frame)),
  )).filter(result => result.status === "fulfilled").map(result => result.value);
  if (storedFrames.length < 3) return { stored: false, reason: "not_enough_usable_nims_frames", eventId: event.id };
  const nearestFrameOffsetMinutes = Math.min(...storedFrames.map(frame => Math.abs(frame.timeOffsetMinutes)));
  await attachGoEventEvidence(event.id, {
    status: "ready",
    source: "USGS NIMS",
    camera: {
      id: match.camera.id,
      name: match.camera.name,
      state: match.camera.state,
      direction: "Fixed view; bearing not published",
      distanceKm: Number(match.distanceKm.toFixed(1)),
      bearingDifference: null,
      viewQuality: match.camera.viewQuality,
      skyScore: match.camera.skyScore,
      horizonSkyPct: match.camera.horizonSkyPct,
      nearestFrameOffsetMinutes,
      intervalMinutes,
    },
    frames: storedFrames,
  });
  return { stored: true, eventId: event.id, frames: storedFrames.length, source: "USGS NIMS" };
}

export async function capturePendingNimsEvidence(limit = 3) {
  const now = Date.now();
  const cameras = await loadNimsWhitelist();
  const pending = (await loadRecentGoEvents(now - 2 * 60 * 60 * 1000, 50))
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => !(event.evidence?.frames || []).length)
    .filter(event => event?.candidateType !== "research_possible" || Number(event?.scanCount || 0) >= 2)
    .filter(event => matchNimsCamera(event, cameras))
    .filter(event => {
      const age = now - new Date(event.lastSeenAt || event.firstSeenAt).getTime();
      return age >= 0 && age <= 2 * 60 * 60 * 1000;
    })
    .sort((a, b) => Number(b.peakScore || 0) - Number(a.peakScore || 0))
    .slice(0, Math.max(0, Math.min(Number(limit) || 3, 5)));
  const results = [];
  for (const event of pending) {
    try { results.push(await captureNimsEvidence(event)); }
    catch (error) { results.push({ stored: false, eventId: event.id, reason: error.message || "nims_capture_failed" }); }
  }
  return results;
}
