import { put } from "@vercel/blob";
import { distKm } from "./alert-common.mjs";
import { attachGoEventEvidence, loadRecentGoEvents } from "./go-event-common.mjs";

const API_ROOT = "https://app.webcoos.org/webcoos/api/v1";
const CREDIT = "WebCOOS / contributing camera partner";
const CATALOG_TTL_MS = 30 * 60 * 1000;
const HEADERS = { "User-Agent": "RainbowConnector/1.0 (noncommercial weather research)" };
let catalogCache = null;
let catalogCacheAt = 0;

function authHeaders() {
  const token = String(process.env.WEBCOOS_API_TOKEN || "").trim();
  if (!token) throw new Error("WEBCOOS_API_TOKEN is not configured");
  return { ...HEADERS, Accept: "application/json", Authorization: `Token ${token}` };
}

export function angleDifference(a, b) {
  return Math.abs((Number(a) - Number(b) + 540) % 360 - 180);
}

function bearing(lat1, lon1, lat2, lon2) {
  const toRad = value => value * Math.PI / 180;
  const p1 = toRad(lat1), p2 = toRad(lat2), dl = toRad(lon2 - lon1);
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

export function wedgeDirection(wedge, lat, lon) {
  const ring = wedge?.coordinates?.[0] || [];
  const bearings = ring.filter(point => Array.isArray(point) && point.length >= 2)
    .filter(point => distKm(lat, lon, Number(point[1]), Number(point[0])) >= 0.2)
    .map(point => bearing(lat, lon, Number(point[1]), Number(point[0])))
    .filter(Number.isFinite);
  if (!bearings.length) return { bearing: null, halfFovDeg: null };
  const x = bearings.reduce((sum, value) => sum + Math.cos(value * Math.PI / 180), 0);
  const y = bearings.reduce((sum, value) => sum + Math.sin(value * Math.PI / 180), 0);
  const center = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  const halfFovDeg = Math.min(90, Math.max(...bearings.map(value => angleDifference(value, center))));
  return { bearing: Number(center.toFixed(1)), halfFovDeg: Number(halfFovDeg.toFixed(1)) };
}

function products(asset) {
  return (asset?.feeds || []).flatMap(feed => feed?.products || []);
}

function currentStillService(asset, nowMs) {
  const services = products(asset).filter(product => product?.data?.common?.slug === "one-minute-stills")
    .flatMap(product => product?.services || []).filter(service => service?.data?.common?.slug);
  return services.sort((a, b) => new Date(b.elements?.last_starting || 0)
    - new Date(a.elements?.last_starting || 0)).find(service => {
      const latest = new Date(service.elements?.last_starting || 0).getTime();
      return Number.isFinite(latest) && nowMs - latest <= 2 * 24 * 60 * 60 * 1000;
    }) || null;
}

export function parseWebcoosAssets(payload, nowMs = Date.now()) {
  return (payload?.results || []).filter(asset => asset?.disposition?.slug === "up").map(asset => {
    const props = asset?.data?.properties || {};
    const location = props.location?.coordinates || [];
    const lat = Number(location[1]), lon = Number(location[0]);
    const service = currentStillService(asset, nowMs);
    const direction = wedgeDirection(props.wedge, lat, lon);
    return {
      id: String(asset?.data?.common?.slug || ""),
      name: asset?.data?.common?.label || asset?.data?.common?.slug,
      state: props.state_or_territory || null,
      lat,
      lon,
      serviceSlug: service?.data?.common?.slug || null,
      latestFrameAt: service?.elements?.last_starting || null,
      pageUrl: asset?.data?.common?.slug ? `https://webcoos.org/cameras/${asset.data.common.slug}/` : "https://webcoos.org/cameras/",
      cameraBearing: direction.bearing,
      halfFovDeg: direction.halfFovDeg,
      viewQuality: "unreviewed",
    };
  }).filter(camera => camera.id && camera.serviceSlug
    && Number.isFinite(camera.lat) && Number.isFinite(camera.lon));
}

export async function loadWebcoosCameras() {
  if (catalogCache && Date.now() - catalogCacheAt < CATALOG_TTL_MS) return catalogCache;
  const response = await fetch(`${API_ROOT}/assets/?page_size=100`, { headers: authHeaders() });
  if (!response.ok) throw new Error(`WebCOOS camera list failed (${response.status})`);
  catalogCache = parseWebcoosAssets(await response.json());
  catalogCacheAt = Date.now();
  return catalogCache;
}

export function matchWebcoosCamera(event, cameras, maxDistanceKm = 35) {
  const rep = event?.representative || {};
  const bowBearing = Number(rep.direction?.bearing);
  if (!Number.isFinite(rep.lat) || !Number.isFinite(rep.lon) || !Number.isFinite(bowBearing)) return null;
  return (cameras || []).map(camera => {
    const distanceKm = distKm(rep.lat, rep.lon, camera.lat, camera.lon);
    const centerDifference = Number.isFinite(camera.cameraBearing)
      ? angleDifference(camera.cameraBearing, bowBearing) : null;
    const directionError = Number.isFinite(centerDifference) && Number.isFinite(camera.halfFovDeg)
      ? Math.max(0, centerDifference - camera.halfFovDeg) : null;
    return { camera, distanceKm, centerDifference, directionError };
  }).filter(match => match.distanceKm <= maxDistanceKm)
    .filter(match => !Number.isFinite(match.directionError) || match.directionError <= 12)
    .sort((a, b) => a.distanceKm + (Number.isFinite(a.directionError) ? a.directionError * 0.5 : 20)
      - (b.distanceKm + (Number.isFinite(b.directionError) ? b.directionError * 0.5 : 20)))[0] || null;
}

export function rankWebcoosElements(payload, centerMs, limit = 5) {
  return (payload?.results || []).map(element => ({
    url: element?.data?.properties?.url,
    size: Number(element?.data?.properties?.size),
    observedAt: element?.data?.extents?.temporal?.min,
  })).filter(frame => /^https:\/\//i.test(String(frame.url || ""))
    && Number.isFinite(new Date(frame.observedAt).getTime()))
    .sort((a, b) => Math.abs(new Date(a.observedAt).getTime() - centerMs)
      - Math.abs(new Date(b.observedAt).getTime() - centerMs))
    .slice(0, Math.max(1, Math.min(Number(limit) || 5, 8)))
    .sort((a, b) => new Date(a.observedAt) - new Date(b.observedAt));
}

async function framesNearEvent(event, match) {
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const params = new URLSearchParams({
    starting_after: new Date(centerMs - 6 * 60 * 1000).toISOString(),
    starting_before: new Date(Math.min(Date.now(), centerMs + 6 * 60 * 1000)).toISOString(),
    service: match.camera.serviceSlug,
    page_size: "20",
  });
  const response = await fetch(`${API_ROOT}/elements/?${params}`, { headers: authHeaders() });
  if (!response.ok) throw new Error(`WebCOOS image list failed (${response.status})`);
  return rankWebcoosElements(await response.json(), centerMs, 5);
}

async function storeFrame(event, match, frame) {
  const response = await fetch(frame.url, { headers: HEADERS });
  if (!response.ok || !String(response.headers.get("content-type") || "").toLowerCase().includes("image/")) {
    throw new Error(`WebCOOS image failed (${response.status})`);
  }
  const bytes = await response.arrayBuffer();
  if (bytes.byteLength < 12_000 || bytes.byteLength > 5_000_000) throw new Error("WebCOOS image size was unsuitable.");
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  const timeOffsetMinutes = Number(((new Date(frame.observedAt).getTime() - centerMs) / 60_000).toFixed(1));
  const stamp = frame.observedAt.replace(/[^0-9]/g, "").slice(0, 17);
  const stored = await put(`go-evidence/${event.id}/webcoos-${match.camera.id}-${stamp}.jpg`, bytes, {
    access: "public", addRandomSuffix: false, allowOverwrite: true, contentType: "image/jpeg",
  });
  return {
    url: stored.url,
    observedAt: frame.observedAt,
    source: CREDIT,
    cameraId: match.camera.id,
    cameraName: match.camera.name,
    cameraPageUrl: match.camera.pageUrl,
    distanceKm: Number(match.distanceKm.toFixed(1)),
    bearingDifference: Number.isFinite(match.directionError) ? Number(match.directionError.toFixed(1)) : null,
    direction: Number.isFinite(match.camera.cameraBearing)
      ? `Fixed view centered ${match.camera.cameraBearing.toFixed(1)}°; ${match.camera.halfFovDeg.toFixed(1)}° half-width`
      : "Fixed view; viewshed bearing unavailable",
    state: match.camera.state,
    viewQuality: match.camera.viewQuality,
    timeOffsetMinutes,
    attribution: CREDIT,
  };
}

export async function captureWebcoosEvidence(event, cameras) {
  if ((event?.evidence?.frames || []).length) return { stored: false, reason: "already_captured", eventId: event.id };
  const match = matchWebcoosCamera(event, cameras);
  if (!match) return { stored: false, reason: "no_webcoos_match", eventId: event.id };
  const centerMs = new Date(event.representative?.detectedAt || event.firstSeenAt).getTime();
  if (Date.now() - centerMs < 4 * 60 * 1000) {
    await attachGoEventEvidence(event.id, {
      status: "waiting_webcoos",
      source: CREDIT,
      camera: {
        id: match.camera.id, name: match.camera.name, state: match.camera.state,
        direction: "Waiting for WebCOOS to index post-GO images",
        distanceKm: Number(match.distanceKm.toFixed(1)),
        bearingDifference: Number.isFinite(match.directionError) ? Number(match.directionError.toFixed(1)) : null,
        viewQuality: match.camera.viewQuality,
      },
    });
    return { stored: false, reason: "waiting_for_webcoos_frames", eventId: event.id };
  }
  const frames = await framesNearEvent(event, match);
  if (frames.length < 3) return { stored: false, reason: "not_enough_webcoos_frames", eventId: event.id, frames: frames.length };
  const storedFrames = (await Promise.allSettled(frames.map(frame => storeFrame(event, match, frame))))
    .filter(result => result.status === "fulfilled").map(result => result.value);
  if (storedFrames.length < 3) return { stored: false, reason: "not_enough_usable_webcoos_frames", eventId: event.id };
  const nearestFrameOffsetMinutes = Math.min(...storedFrames.map(frame => Math.abs(frame.timeOffsetMinutes)));
  await attachGoEventEvidence(event.id, {
    status: "ready",
    source: CREDIT,
    camera: {
      id: match.camera.id,
      name: match.camera.name,
      state: match.camera.state,
      direction: storedFrames[0].direction,
      distanceKm: storedFrames[0].distanceKm,
      bearingDifference: storedFrames[0].bearingDifference,
      viewQuality: match.camera.viewQuality,
      nearestFrameOffsetMinutes,
      intervalMinutes: 1,
      cameraBearing: match.camera.cameraBearing,
      halfFovDeg: match.camera.halfFovDeg,
      attribution: CREDIT,
    },
    frames: storedFrames,
  });
  return { stored: true, eventId: event.id, frames: storedFrames.length, source: CREDIT };
}

export function selectPendingWebcoosEvents(events, limit = 2) {
  return (events || [])
    .slice(0, Math.max(0, Math.min(Number(limit) || 2, 4)));
}
export async function capturePendingWebcoosEvidence(limit = 2) {
  const now = Date.now();
  const cameras = await loadWebcoosCameras();
  const pending = selectPendingWebcoosEvents((await loadRecentGoEvents(now - 45 * 60 * 1000, 50))
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => !(event.evidence?.frames || []).length)
    .filter(event => event.evidence?.status !== "waiting_usgs")
    .filter(event => matchWebcoosCamera(event, cameras))
    .sort((a, b) => Number(b.peakScore || 0) - Number(a.peakScore || 0))
    , limit);
  const results = [];
  for (const event of pending) {
    try { results.push(await captureWebcoosEvidence(event, cameras)); }
    catch (error) { results.push({ stored: false, eventId: event.id, reason: error.message || "webcoos_capture_failed" }); }
  }
  return results;
}
