import { put } from "@vercel/blob";
import { distKm } from "./alert-common.mjs";
import { attachGoEventEvidence, loadRecentGoEvents } from "./go-event-common.mjs";

const ALERTCA_QUERY = "https://services8.arcgis.com/X84q166Srnyl4JMV/ArcGIS/rest/services/ALERTCalifornia_Camera_Feed/FeatureServer/0/query";
const ALERTCA_CREDIT = "ALERTCalifornia | UC San Diego";
const HEADERS = { "User-Agent": "RainbowConnector/1.0 (noncommercial weather research)" };

export function angleDifference(a, b) {
  return Math.abs((Number(a) - Number(b) + 540) % 360 - 180);
}

function safeHttps(value) {
  return /^https:\/\//i.test(String(value || "")) ? String(value) : null;
}

export function parseAlertCaFeatures(payload) {
  return (payload?.features || []).map(feature => {
    const row = feature.attributes || {};
    const observed = new Date(row.viewTime);
    return {
      id: `alertca-${row.OBJECTID}`,
      name: row.cameraName || `ALERTCalifornia camera ${row.OBJECTID}`,
      siteId: row.siteId || null,
      lat: Number(feature.geometry?.y),
      lon: Number(feature.geometry?.x),
      pan: Number(row.positionPan),
      tilt: Number(row.positionTilt),
      zoomScale: Number(row.viewZoomScale),
      observedAt: Number.isFinite(observed.getTime()) ? observed.toISOString() : null,
      imageUrl: safeHttps(row.imageURL),
      pageUrl: safeHttps(row.cameraURL) || "https://cameras.alertcalifornia.org/",
    };
  }).filter(camera => camera.id !== "alertca-undefined" && camera.imageUrl
    && Number.isFinite(camera.lat) && Number.isFinite(camera.lon)
    && Number.isFinite(camera.pan));
}

export async function loadAlertCaCameras() {
  const query = new URLSearchParams({
    where: "isOnline='online'",
    outFields: "OBJECTID,cameraName,siteId,cameraURL,positionPan,positionTilt,viewZoomScale,viewTime,imageURL",
    returnGeometry: "true",
    outSR: "4326",
    resultRecordCount: "2000",
    f: "json",
  });
  const response = await fetch(`${ALERTCA_QUERY}?${query}`, { headers: HEADERS, cache: "no-store" });
  if (!response.ok) throw new Error(`ALERTCalifornia camera list failed (${response.status})`);
  return parseAlertCaFeatures(await response.json());
}

export function matchAlertCaCameras(event, cameras, options = {}) {
  const rep = event?.representative || {};
  const bearing = Number(rep.direction?.bearing);
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon) || !Number.isFinite(bearing)) return [];
  const maxDistanceKm = Number(options.maxDistanceKm || 35);
  const maxBearingDifference = Number(options.maxBearingDifference || 32);
  const limit = Math.max(1, Math.min(Number(options.limit) || 3, 5));
  return (cameras || []).map(camera => ({
    camera,
    distanceKm: distKm(rep.lat, rep.lon, camera.lat, camera.lon),
    bearingDifference: angleDifference(camera.pan, bearing),
  })).filter(match => match.distanceKm <= maxDistanceKm && match.bearingDifference <= maxBearingDifference)
    .sort((a, b) => a.distanceKm + a.bearingDifference * 0.35
      - (b.distanceKm + b.bearingDifference * 0.35)).slice(0, limit);
}

export function nearbyAlertCaCameras(event, cameras, maxDistanceKm = 35) {
  const rep = event?.representative || {};
  const bearing = Number(rep.direction?.bearing);
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon) || !Number.isFinite(bearing)) return [];
  return (cameras || []).map(camera => ({
    camera,
    distanceKm: distKm(rep.lat, rep.lon, camera.lat, camera.lon),
    bearingDifference: angleDifference(camera.pan, bearing),
  })).filter(match => match.distanceKm <= maxDistanceKm)
    .sort((a, b) => a.distanceKm - b.distanceKm);
}

async function storeFrame(event, match) {
  const response = await fetch(`${match.camera.imageUrl}?rc=${Date.now()}`, {
    headers: HEADERS,
    cache: "no-store",
  });
  if (!response.ok || !String(response.headers.get("content-type") || "").toLowerCase().includes("image/")) {
    throw new Error(`ALERTCalifornia image failed (${response.status})`);
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 12_000 || bytes.byteLength > 5_000_000) throw new Error("ALERTCalifornia image size was unsuitable.");
  const observedAt = match.camera.observedAt || new Date().toISOString();
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const timeOffsetMinutes = Number(((new Date(observedAt).getTime() - centerMs) / 60_000).toFixed(1));
  const stamp = observedAt.replace(/[^0-9]/g, "").slice(0, 17);
  const stored = await put(`go-evidence/${event.id}/alertca-${match.camera.id}-${stamp}.jpg`, bytes, {
    access: "public", addRandomSuffix: false, allowOverwrite: true, contentType: "image/jpeg",
  });
  return {
    url: stored.url,
    observedAt,
    source: ALERTCA_CREDIT,
    cameraId: match.camera.id,
    cameraName: match.camera.name,
    cameraPageUrl: match.camera.pageUrl,
    distanceKm: Number(match.distanceKm.toFixed(1)),
    bearingDifference: Number(match.bearingDifference.toFixed(1)),
    direction: `Camera faced ${match.camera.pan.toFixed(1)}° at capture`,
    state: "CA",
    viewQuality: "usable",
    cameraPan: Number(match.camera.pan.toFixed(1)),
    cameraTilt: Number.isFinite(match.camera.tilt) ? Number(match.camera.tilt.toFixed(1)) : null,
    zoomScale: Number.isFinite(match.camera.zoomScale) ? match.camera.zoomScale : null,
    timeOffsetMinutes,
    attribution: ALERTCA_CREDIT,
  };
}

export async function captureAlertCaEvidence(event, cameras) {
  if ((event?.evidence?.frames || []).length) return { stored: false, reason: "already_captured", eventId: event.id };
  const nearby = nearbyAlertCaCameras(event, cameras);
  if (!nearby.length) return { stored: false, reason: "no_alertca_match", eventId: event.id };
  const aligned = matchAlertCaCameras(event, cameras);
  if (!aligned.length) {
    const nearest = nearby[0];
    await attachGoEventEvidence(event.id, {
      status: "waiting_alertca",
      source: ALERTCA_CREDIT,
      camera: {
        id: nearest.camera.id,
        name: nearest.camera.name,
        state: "CA",
        direction: "Waiting for the sweeping camera to face the predicted rainbow",
        distanceKm: Number(nearest.distanceKm.toFixed(1)),
        bearingDifference: Number(nearest.bearingDifference.toFixed(1)),
        viewQuality: "usable",
      },
    });
    return { stored: false, reason: "waiting_for_aligned_alertca_view", eventId: event.id };
  }
  const storedFrames = (await Promise.allSettled(aligned.map(match => storeFrame(event, match))))
    .filter(result => result.status === "fulfilled").map(result => result.value);
  if (!storedFrames.length) return { stored: false, reason: "no_usable_alertca_frames", eventId: event.id };
  const primary = storedFrames.sort((a, b) => a.distanceKm + a.bearingDifference * 0.35
    - (b.distanceKm + b.bearingDifference * 0.35))[0];
  const nearestFrameOffsetMinutes = Math.min(...storedFrames.map(frame => Math.abs(frame.timeOffsetMinutes)));
  await attachGoEventEvidence(event.id, {
    status: "ready",
    source: ALERTCA_CREDIT,
    camera: {
      id: primary.cameraId,
      name: primary.cameraName,
      state: "CA",
      direction: primary.direction,
      distanceKm: primary.distanceKm,
      bearingDifference: primary.bearingDifference,
      viewQuality: "usable",
      nearestFrameOffsetMinutes,
      attribution: ALERTCA_CREDIT,
    },
    frames: storedFrames,
  });
  return { stored: true, eventId: event.id, frames: storedFrames.length, source: ALERTCA_CREDIT };
}

export function selectPendingAlertCaEvents(events, limit = 2) {
  return (events || [])
    .filter(event => event?.candidateType !== "research_possible" || Number(event?.scanCount || 0) >= 2)
    .slice(0, Math.max(0, Math.min(Number(limit) || 2, 4)));
}

export async function capturePendingAlertCaEvidence(limit = 2) {
  const now = Date.now();
  const cameras = await loadAlertCaCameras();
  const pending = selectPendingAlertCaEvents((await loadRecentGoEvents(now - 45 * 60 * 1000, 50))
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => !(event.evidence?.frames || []).length)
    .filter(event => !["waiting_usgs", "waiting_webcoos"].includes(event.evidence?.status))
    .filter(event => nearbyAlertCaCameras(event, cameras).length)
    .sort((a, b) => Number(b.peakScore || 0) - Number(a.peakScore || 0)), limit);
  const results = [];
  for (const event of pending) {
    try { results.push(await captureAlertCaEvidence(event, cameras)); }
    catch (error) { results.push({ stored: false, eventId: event.id, reason: error.message || "alertca_capture_failed" }); }
  }
  return results;
}
