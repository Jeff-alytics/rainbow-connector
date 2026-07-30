import { json, readJsonBody, verifySecret } from "./alert-common.mjs";
import {
  deleteHistoricalReviewEvent,
  saveHistoricalReviewEvent,
} from "./go-event-common.mjs";
import {
  captureWebcoosEvidence,
  loadWebcoosCameras,
} from "./webcoos-common.mjs";

export const config = { maxDuration: 60 };

function finite(value, fallback = null) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    json(res, 405, { ok: false, error: "Method not allowed." });
    return;
  }
  const body = await readJsonBody(req);
  if (!verifySecret(req, body)) {
    json(res, 401, { ok: false, error: "Unauthorized." });
    return;
  }
  const input = body?.candidate || body;
  const observedAt = new Date(input?.observedAt || 0);
  const bowBearing = finite(input?.bowBearing);
  const cameraId = String(input?.cameraId || "").trim();
  if (!cameraId || !Number.isFinite(observedAt.getTime()) || !Number.isFinite(bowBearing)
    || observedAt.getTime() > Date.now() - 24 * 60 * 60 * 1000
    || observedAt.getTime() < new Date("2021-01-01T00:00:00Z").getTime()) {
    json(res, 400, { ok: false, error: "Invalid historical WebCOOS candidate." });
    return;
  }
  const cameras = await loadWebcoosCameras();
  const camera = cameras.find(item => item.id === cameraId);
  if (!camera) {
    json(res, 404, { ok: false, error: "Active WebCOOS camera was not found." });
    return;
  }
  const saved = await saveHistoricalReviewEvent({
    sourceKey: `webcoos:${camera.id}:${observedAt.toISOString()}`,
    cameraId: camera.id,
    cameraName: camera.name,
    label: camera.name,
    observedAt: observedAt.toISOString(),
    lat: camera.lat,
    lon: camera.lon,
    bowBearing,
    score: Math.max(0, Math.min(100, finite(input.score, 0))),
    rank: finite(input.rank),
    sunElevationDeg: finite(input.sunElevationDeg),
    rainbowArcDeg: finite(input.rainbowArcDeg),
    directNormalIrradianceWm2: finite(input.directNormalIrradianceWm2),
    cloudCoverPct: finite(input.cloudCoverPct),
    rainIntensity: finite(input.rainIntensity),
    observerRainIntensity: finite(input.observerRainIntensity),
    rainPoint: input.rainPoint || null,
    radar: input.radar || null,
    archiveMethod: "Open-Meteo hourly discovery + IEM NEXRAD anti-solar fan refinement",
  });
  const event = saved.event;
  if (!event) {
    json(res, 500, { ok: false, error: "Historical event could not be stored." });
    return;
  }
  if ((event.evidence?.frames || []).length) {
    json(res, 200, { ok: true, duplicate: true, eventId: event.id, frames: event.evidence.frames.length });
    return;
  }
  const capture = await captureWebcoosEvidence(event, [camera]);
  if (!capture.stored) {
    if (saved.stored) await deleteHistoricalReviewEvent(event.id);
    json(res, 422, { ok: false, error: capture.reason || "WebCOOS archive frames were unavailable.", capture });
    return;
  }
  json(res, 200, {
    ok: true,
    duplicate: saved.reason === "duplicate",
    eventId: event.id,
    frames: capture.frames,
    camera: camera.name,
  });
}
