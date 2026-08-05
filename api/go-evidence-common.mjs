import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { put } from "@vercel/blob";
import { distKm } from "./alert-common.mjs";
import { attachGoEventEvidence, loadRecentGoEvents } from "./go-event-common.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const FAA_API = "https://weathercams.faa.gov/api";
const FAA_HEADERS = {
  Referer: "https://weathercams.faa.gov/",
  Origin: "https://weathercams.faa.gov",
  "User-Agent": "Mozilla/5.0 rainbow-connector-review",
};
let siteCache = null;

async function sites() {
  if (!siteCache) siteCache = JSON.parse(await readFile(path.join(ROOT, "faa-sites-compact.json"), "utf8"));
  return siteCache;
}

function angleDifference(a, b) {
  return Math.abs((a - b + 540) % 360 - 180);
}

const FAA_MATCHER_VERSION = "faa-bow-arc-balanced-window-2026-07-v2";
const DEFAULT_FAA_FOV_DEG = 45;
const POSSIBLE_FAA_MAX_DISTANCE_KM = 40;

function normalizeBearing(value) { return (Number(value) % 360 + 360) % 360; }

function intervalSegments(center, width) {
  const half = width / 2, start = normalizeBearing(center - half), end = normalizeBearing(center + half);
  return start <= end ? [[start, end]] : [[start, 360], [0, end]];
}

export function angularIntervalOverlapDeg(aCenter, aWidth, bCenter, bWidth) {
  let overlap = 0;
  for (const a of intervalSegments(aCenter, aWidth)) for (const b of intervalSegments(bCenter, bWidth)) {
    overlap += Math.max(0, Math.min(a[1], b[1]) - Math.max(a[0], b[0]));
  }
  return overlap;
}

export function visibleBowArc(event) {
  const rep = event?.representative || {}, elevation = Number(rep.evidence?.sunElevationDeg);
  const antiSolarBearing = Number(rep.direction?.bearing);
  if (!Number.isFinite(elevation) || !Number.isFinite(antiSolarBearing) || elevation < -0.833 || elevation >= 42) return null;
  const ratio = Math.cos(42 * Math.PI / 180) / Math.cos(elevation * Math.PI / 180);
  const halfExtentDeg = Math.acos(Math.max(-1, Math.min(1, ratio))) * 180 / Math.PI;
  return { centerBearing: normalizeBearing(antiSolarBearing), halfExtentDeg, widthDeg: halfExtentDeg * 2 };
}

export function matchFaaCameras(event, catalog, options = {}) {
  const rep = event?.representative || {}, arc = visibleBowArc(event);
  const maxDistanceKm = Number(options.maxDistanceKm ?? 80), limit = Number(options.limit ?? 3);
  const minOverlapDeg = Number(options.minOverlapDeg ?? 3);
  if (!arc || !Number.isFinite(rep.lat) || !Number.isFinite(rep.lon)) return [];
  const matches = [];
  for (const site of catalog || []) {
    const distanceKm = distKm(rep.lat, rep.lon, site.lat, site.lon);
    if (distanceKm > maxDistanceKm) continue;
    for (const camera of site.cameras || []) {
      const fieldOfViewDeg = Number(camera.mapWedgeAngle || camera.fieldOfViewDeg || DEFAULT_FAA_FOV_DEG);
      const bowArcOverlapDeg = angularIntervalOverlapDeg(camera.bearing, fieldOfViewDeg, arc.centerBearing, arc.widthDeg);
      if (bowArcOverlapDeg < minOverlapDeg) continue;
      const visibleBowFraction = arc.widthDeg ? bowArcOverlapDeg / arc.widthDeg : 0;
      const bearingDifference = angleDifference(camera.bearing, arc.centerBearing);
      const score = distanceKm - visibleBowFraction * 70 + bearingDifference * 0.05;
      matches.push({ site, camera, distanceKm, bearingDifference, score, fieldOfViewDeg,
        bowArcOverlapDeg, visibleBowFraction, bowArc: arc, matcherVersion: FAA_MATCHER_VERSION });
    }
  }
  const ranked = matches.sort((a, b) => a.score - b.score || a.distanceKm - b.distanceKm);
  const maximum = Math.max(1, Math.min(limit || 3, 6)), selected = ranked.length ? [ranked[0]] : [];
  if (maximum > 1 && selected.length) {
    const oppositeLeg = ranked.slice(1)
      .filter(match => angleDifference(match.camera.bearing, selected[0].camera.bearing) >= 45)
      .sort((a, b) => b.visibleBowFraction - a.visibleBowFraction || a.distanceKm - b.distanceKm)[0];
    if (oppositeLeg) selected.push(oppositeLeg);
  }
  for (const match of ranked) {
    if (selected.length >= maximum) break;
    if (!selected.includes(match)) selected.push(match);
  }
  return selected;
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
  return selectFaaFrames(payload.payload, match.camera.id, centerMs);
}

export function selectFaaFrames(payload, cameraId, centerMs) {
  const eligible = (Array.isArray(payload) ? payload : [])
    .filter(image => String(image.cameraId) === String(cameraId) && image.imageUri)
    .filter(image => Number.isFinite(new Date(image.imageDatetime).getTime()));
  const before = eligible
    .filter(image => new Date(image.imageDatetime).getTime() <= centerMs)
    .sort((a, b) => new Date(b.imageDatetime) - new Date(a.imageDatetime))
    .slice(0, 2);
  const after = eligible
    .filter(image => new Date(image.imageDatetime).getTime() > centerMs)
    .sort((a, b) => new Date(a.imageDatetime) - new Date(b.imageDatetime))
    .slice(0, 3);
  return [...before, ...after].sort((a, b) => new Date(a.imageDatetime) - new Date(b.imageDatetime));
}

function hasCompleteFaaWindow(event) {
  const frames = event?.evidence?.source === "FAA WeatherCam" ? event.evidence.frames || [] : [];
  return frames.length >= 4 && frames.filter(frame => Number(frame.timeOffsetMinutes) > 0).length >= 2;
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
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const observedMs = new Date(frame.imageDatetime || 0).getTime();
  const visibleBowFraction = Number(match.visibleBowFraction);
  return {
    url: stored.url, observedAt: frame.imageDatetime || null, source: "FAA WeatherCam",
    cameraName: `${match.site.name} · ${match.camera.direction || match.camera.bearing + "°"}`,
    siteId: match.site.id, cameraId: match.camera.id, distanceKm: Number(match.distanceKm.toFixed(1)),
    bearingDifference: Number(match.bearingDifference.toFixed(1)),
    bowArcOverlapDeg: Number.isFinite(match.bowArcOverlapDeg) ? Number(match.bowArcOverlapDeg.toFixed(1)) : null,
    visibleBowFraction: Number.isFinite(visibleBowFraction) ? Number(visibleBowFraction.toFixed(3)) : null,
    fieldOfViewDeg: Number.isFinite(match.fieldOfViewDeg) ? match.fieldOfViewDeg : null,
    viewQuality: Number.isFinite(visibleBowFraction) && visibleBowFraction >= 0.5 && match.distanceKm <= 35 ? "usable" : "limited",
    negativeEvidenceEligible: Number.isFinite(visibleBowFraction) && visibleBowFraction >= 0.5 && match.distanceKm <= 35,
    timeOffsetMinutes: Number.isFinite(observedMs) && Number.isFinite(centerMs)
      ? Number(((observedMs - centerMs) / 60000).toFixed(1)) : null,
  };
}

export async function captureFaaEvidence(event) {
  const centerMs = new Date(event?.representative?.detectedAt || event?.firstSeenAt || 0).getTime();
  if (!Number.isFinite(centerMs)) return { stored: false, reason: "invalid_event_time", eventId: event?.id };
  if (event?.evidence?.matcherVersion === FAA_MATCHER_VERSION && hasCompleteFaaWindow(event)) {
    return { stored: false, reason: "already_captured", eventId: event.id };
  }
  const catalog = await sites();
  const possible = event?.candidateClass === "POSSIBLE"
    || ["research_possible", "live_possible"].includes(event?.candidateType);
  const arcMatches = matchFaaCameras(event, catalog, {
    maxDistanceKm: possible ? POSSIBLE_FAA_MAX_DISTANCE_KM : 80,
    limit: 3,
  });
  const legacy = arcMatches.length ? null : matchFaaCamera(event, catalog);
  const matches = arcMatches.length ? arcMatches : legacy ? [legacy] : [];
  if (!matches.length) {
    await attachGoEventEvidence(event.id, { status: "no_camera_match", source: "FAA WeatherCam",
      matcherVersion: FAA_MATCHER_VERSION });
    return { stored: false, reason: "no_camera_match", eventId: event.id };
  }
  const captured = await Promise.all(matches.map(async match => {
    const frames = await faaFrames(event, match);
    const stored = (await Promise.all(frames.map(frame => storeFrame(event, match, frame).catch(() => null)))).filter(Boolean);
    return { match, stored };
  }));
  const usableCaptured = captured.filter(item => item.stored.length > 0);
  const storedFrames = usableCaptured.flatMap(item => item.stored);
  const usedMatches = usableCaptured.map(item => item.match);
  if (!storedFrames.length) return { stored: false, reason: "awaiting_event_frames", eventId: event.id, frames: 0 };
  const primary = usedMatches[0] || matches[0];
  await attachGoEventEvidence(event.id, {
    status: storedFrames.filter(frame => Number(frame.timeOffsetMinutes) > 0).length >= 2 ? "ready" : "collecting",
    source: "FAA WeatherCam",
    matcherVersion: FAA_MATCHER_VERSION,
    camera: {
      siteId: primary.site.id, cameraId: primary.camera.id, name: primary.site.name,
      state: primary.site.state, direction: primary.camera.direction,
      distanceKm: Number(primary.distanceKm.toFixed(1)), bearingDifference: Number(primary.bearingDifference.toFixed(1)),
      bowArcOverlapDeg: Number.isFinite(primary.bowArcOverlapDeg) ? Number(primary.bowArcOverlapDeg.toFixed(1)) : null,
      visibleBowFraction: Number.isFinite(primary.visibleBowFraction) ? Number(primary.visibleBowFraction.toFixed(3)) : null,
      viewQuality: Number.isFinite(primary.visibleBowFraction) && primary.visibleBowFraction >= 0.5 && primary.distanceKm <= 35 ? "usable" : "limited",
    },
    cameras: usedMatches.map(match => ({ siteId: match.site.id, cameraId: match.camera.id,
      name: match.site.name, state: match.site.state, direction: match.camera.direction,
      distanceKm: Number(match.distanceKm.toFixed(1)), bearingDifference: Number(match.bearingDifference.toFixed(1)),
      bowArcOverlapDeg: Number.isFinite(match.bowArcOverlapDeg) ? Number(match.bowArcOverlapDeg.toFixed(1)) : null,
      visibleBowFraction: Number.isFinite(match.visibleBowFraction) ? Number(match.visibleBowFraction.toFixed(3)) : null })),
    frames: storedFrames,
  });
  return { stored: true, eventId: event.id, frames: storedFrames.length, cameras: usedMatches.length,
    matcherVersion: FAA_MATCHER_VERSION };
}

export function selectPendingFaaEvents(ranked, limit = 2) {
  ranked = ranked || [];
  const maximum = Math.max(0, Math.min(Number(limit) || 2, 4));
  const research = maximum >= 2 ? ranked.find(event => event.candidateType === "research_possible") : null;
  const pending = ranked.filter(event => event !== research && event.candidateType !== "research_possible")
    .slice(0, Math.max(0, maximum - (research ? 1 : 0)));
  if (research) pending.push(research);
  for (const event of ranked) {
    if (pending.length >= maximum) break;
    if (!pending.includes(event)) pending.push(event);
  }
  return pending;
}

export async function capturePendingFaaEvidence(limit = 2) {
  const now = Date.now();
  const ranked = (await loadRecentGoEvents(now - 2 * 60 * 60 * 1000, 50))
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => !(event.evidence?.status === "no_camera_match"
      && event.evidence?.matcherVersion === FAA_MATCHER_VERSION))
    .filter(event => !hasCompleteFaaWindow(event)
      || event.evidence?.matcherVersion !== FAA_MATCHER_VERSION)
    .filter(event => {
      const age = now - new Date(event.lastSeenAt || event.firstSeenAt).getTime();
      return age >= 0 && age <= 2 * 60 * 60 * 1000;
    })
    .sort((a, b) => Number(b.peakScore || 0) - Number(a.peakScore || 0));
  const pending = selectPendingFaaEvents(ranked, limit);
  const results = [];
  for (const event of pending) {
    try { results.push(await captureFaaEvidence(event)); }
    catch (error) { results.push({ stored: false, eventId: event.id, reason: error.message || "capture_failed" }); }
  }
  return results;
}
