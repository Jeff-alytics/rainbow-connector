import { put } from "@vercel/blob";
import { json, readJsonBody, verifySecret } from "./alert-common.mjs";
import { dotCameraJobs } from "./dot-camera-common.mjs";
import { attachGoEventEvidence, loadRecentGoEvents } from "./go-event-common.mjs";

export const config = { maxDuration: 60 };

async function pendingJobs() {
  const now = Date.now();
  const events = (await loadRecentGoEvents(now - 45 * 60 * 1000, 30))
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => !(event.evidence?.frames || []).length)
    .filter(event => event.evidence?.status !== "waiting_usgs")
    .filter(event => {
      if (!["waiting_alertca", "waiting_webcoos"].includes(event.evidence?.status)) return true;
      const age = now - new Date(event.firstSeenAt || event.lastSeenAt).getTime();
      return age >= 30 * 60 * 1000;
    })
    .sort((a, b) => Number(b.peakScore || 0) - Number(a.peakScore || 0))
    .slice(0, 3);
  return dotCameraJobs(events, { maxDistanceKm: 35, camerasPerEvent: 3 });
}

function cleanFrame(frame) {
  const bytes = Buffer.from(String(frame?.imageBase64 || ""), "base64");
  if (!/^go-[a-zA-Z0-9-]+$/.test(String(frame?.eventId || ""))) throw new Error("Invalid event ID.");
  if (!/^[a-zA-Z0-9_-]+$/.test(String(frame?.cameraId || ""))) throw new Error("Invalid camera ID.");
  if (!/^[a-zA-Z0-9 ._-]{2,40}$/.test(String(frame?.source || ""))) throw new Error("Invalid camera source.");
  if (bytes.length < 8000 || bytes.length > 1_500_000) throw new Error("Invalid camera image size.");
  return { ...frame, bytes };
}

async function storeFrames(body) {
  const frames = (Array.isArray(body.frames) ? body.frames : []).slice(0, 9).map(cleanFrame);
  const byEvent = new Map();
  for (const frame of frames) {
    const observedAt = frame.observedAt || new Date().toISOString();
    const stamp = String(observedAt).replace(/[^0-9]/g, "").slice(0, 17);
    const sourceSlug = String(frame.source).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    const stored = await put(`go-evidence/${frame.eventId}/${sourceSlug}-${frame.cameraId}-${stamp}.jpg`, frame.bytes, {
      access: "public", addRandomSuffix: false, allowOverwrite: true, contentType: "image/jpeg",
    });
    const result = {
      url: stored.url,
      observedAt,
      source: frame.source,
      cameraName: String(frame.cameraName || frame.cameraId).slice(0, 160),
      cameraPageUrl: frame.cameraPageUrl || null,
      distanceKm: Number.isFinite(Number(frame.distanceKm)) ? Number(Number(frame.distanceKm).toFixed(1)) : null,
      bearingDifference: Number.isFinite(Number(frame.bearingDifference)) ? Number(frame.bearingDifference) : null,
      direction: frame.direction || null,
      state: frame.state || null,
    };
    if (!byEvent.has(frame.eventId)) byEvent.set(frame.eventId, []);
    byEvent.get(frame.eventId).push(result);
  }
  for (const [eventId, stored] of byEvent) {
    const primary = stored[0];
    await attachGoEventEvidence(eventId, {
      status: "ready",
      source: primary?.source || "DOT camera",
      camera: {
        name: `${primary?.source || "DOT"} nearby live views`,
        state: primary?.state || null,
        direction: primary?.direction || "Current view may change; bearing not published",
        distanceKm: primary?.distanceKm ?? null,
        bearingDifference: primary?.bearingDifference ?? null,
      },
      frames: stored,
    });
  }
  return { stored: frames.length, events: byEvent.size };
}

export default async function handler(req, res) {
  if (!["GET", "POST"].includes(req.method)) {
    res.setHeader("Allow", "GET, POST");
    return json(res, 405, { ok: false, error: "Method not allowed." });
  }
  const body = req.method === "POST" ? await readJsonBody(req) : {};
  if (!verifySecret(req, body)) return json(res, 401, { ok: false, error: "Unauthorized." });
  try {
    if (req.method === "GET") return json(res, 200, { ok: true, jobs: await pendingJobs() });
    return json(res, 200, { ok: true, ...(await storeFrames(body)) });
  } catch (error) {
    return json(res, 500, { ok: false, error: error.message || "DOT evidence failed." });
  }
}
