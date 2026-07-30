import { json, readJsonBody, verifySecret } from "./alert-common.mjs";
import { REVIEW_LABELS, labelGoEvent, loadGoEvents } from "./go-event-common.mjs";
import { hasReviewSession } from "./review-auth-common.mjs";

export const config = { maxDuration: 10 };

function authorized(req, body = {}) {
  return hasReviewSession(req) || verifySecret(req, body);
}

export function reviewQueue(events) {
  const strengthRank = { strong: 3, usable: 2, limited: 1, unknown: 0 };
  return [...(events || [])]
    .filter(event => (event.review?.label || "pending") === "pending")
    .filter(event => (event.evidence?.frames || []).length > 0)
    .sort((a, b) => {
      const aClass = eventClass(a) === "GO" ? 1 : 0;
      const bClass = eventClass(b) === "GO" ? 1 : 0;
      const aCamera = a.evidence?.camera || {}, bCamera = b.evidence?.camera || {};
      const aStrength = reviewEvidenceStrength(aCamera.distanceKm, aCamera.bearingDifference, aCamera.viewQuality, aCamera.nearestFrameOffsetMinutes);
      const bStrength = reviewEvidenceStrength(bCamera.distanceKm, bCamera.bearingDifference, bCamera.viewQuality, bCamera.nearestFrameOffsetMinutes);
      return bClass - aClass
      || strengthRank[bStrength] - strengthRank[aStrength]
      || Number(b.peakScore || 0) - Number(a.peakScore || 0)
      || new Date(b.lastSeenAt || 0) - new Date(a.lastSeenAt || 0);
    });
}

function eventClass(event) {
  if (event?.candidateClass === "POSSIBLE" || event?.candidateType === "live_possible") return "POSSIBLE";
  if (event?.candidateType === "historical_webcoos") return "ARCHIVE";
  return "GO";
}

export function reviewEvidenceStrength(distanceKm, bearingDifference, viewQuality = "usable", frameTimeErrorMinutes = null) {
  if (!Number.isFinite(distanceKm) || !Number.isFinite(bearingDifference)) return "unknown";
  if (["limited", "unreviewed"].includes(viewQuality)
    || (Number.isFinite(frameTimeErrorMinutes) && frameTimeErrorMinutes > 8)) return "limited";
  if (distanceKm <= 25 && bearingDifference <= 15) return "strong";
  if (distanceKm <= 35 && bearingDifference <= 30) return "usable";
  return "limited";
}

export function reviewResults(events) {
  return [...(events || [])]
    .filter(event => event.review?.label && event.review.label !== "pending")
    .sort((a, b) => new Date(b.review?.reviewedAt || 0) - new Date(a.review?.reviewedAt || 0))
    .map(event => {
      const rep = event.representative || {};
      const evidence = rep.evidence || {};
      const camera = event.evidence?.camera || {};
      return {
        id: event.id,
        candidateType: event.candidateType || "live_go",
        candidateClass: eventClass(event),
        archiveMethod: evidence.archiveMethod || null,
        grade: event.review.label,
        reviewedAt: event.review.reviewedAt || null,
        notes: event.review.notes || null,
        sceneSunlight: event.review.sceneSunlight || null,
        sceneSunlight: event.review.sceneSunlight || null,
        detectedAt: rep.detectedAt || event.firstSeenAt || null,
        score: Number.isFinite(event.peakScore) ? event.peakScore : null,
        scanCount: Number.isFinite(rep.persistence?.scanCount)
          ? rep.persistence.scanCount
          : Number.isFinite(event.scanCount) ? event.scanCount : null,
        sunElevationDeg: Number.isFinite(evidence.sunElevationDeg) ? evidence.sunElevationDeg : null,
        rainbowArcDeg: Number.isFinite(evidence.rainbowArcDeg)
          ? evidence.rainbowArcDeg
          : Number.isFinite(evidence.sunElevationDeg) ? Math.max(0, 42 - evidence.sunElevationDeg) : null,
        directNormalIrradianceWm2: Number.isFinite(evidence.directNormalIrradianceWm2)
          ? evidence.directNormalIrradianceWm2
          : Number.isFinite(evidence.directRadiationWm2) ? evidence.directRadiationWm2 : null,
        rainIntensity: Number.isFinite(evidence.rainIntensity) ? evidence.rainIntensity : null,
        observerRainIntensity: Number.isFinite(evidence.observerRainIntensity) ? evidence.observerRainIntensity : null,
        camera: {
          name: camera.name || null,
          source: event.evidence?.source || null,
          distanceKm: Number.isFinite(camera.distanceKm) ? camera.distanceKm : null,
          bearingDifference: Number.isFinite(camera.bearingDifference) ? camera.bearingDifference : null,
          viewQuality: camera.viewQuality || null,
          horizonSkyPct: Number.isFinite(camera.horizonSkyPct) ? camera.horizonSkyPct : null,
          nearestFrameOffsetMinutes: Number.isFinite(camera.nearestFrameOffsetMinutes) ? camera.nearestFrameOffsetMinutes : null,
        },
        reviewStrength: reviewEvidenceStrength(camera.distanceKm, camera.bearingDifference, camera.viewQuality, camera.nearestFrameOffsetMinutes),
        sunlightAssessment: (event.researchAssessments || []).at(-1) || null,
      };
    });
}

export default async function handler(req, res) {
  if (req.method === "GET") {
    if (!authorized(req)) { json(res, 401, { ok: false, error: "Unauthorized." }); return; }
    const limit = Math.max(1, Math.min(Number(req.query?.limit) || 100, 1000));
    const label = String(req.query?.label || "").trim();
    const queue = String(req.query?.queue || "") === "1";
    const results = String(req.query?.results || "") === "1";
    const loaded = await loadGoEvents(limit);
    const events = queue
      ? reviewQueue(loaded)
      : loaded.filter(event => !label || event.review?.label === label)
        .filter(event => !results || (event.review?.label && event.review.label !== "pending"));
    const counts = {};
    for (const event of events) counts[event.review?.label || "pending"] = (counts[event.review?.label || "pending"] || 0) + 1;
    const items = queue ? events.map(event => ({
      id: event.id,
      candidateType: event.candidateType || "live_go",
      candidateClass: eventClass(event),
      detectedAt: event.representative?.detectedAt || event.firstSeenAt || null,
      frames: (event.evidence?.frames || []).map(frame => ({
        url: frame.url,
        observedAt: frame.observedAt || null,
        source: frame.source || event.evidence?.source || null,
        cameraName: frame.cameraName || null,
        distanceKm: Number.isFinite(frame.distanceKm) ? frame.distanceKm : null,
        viewQuality: frame.viewQuality || null,
        timeOffsetMinutes: Number.isFinite(frame.timeOffsetMinutes) ? frame.timeOffsetMinutes : null,
      })),
      camera: event.evidence?.camera ? {
        name: event.evidence.camera.name || null,
        direction: event.evidence.camera.direction || null,
        source: event.evidence.source || null,
        distanceKm: Number.isFinite(event.evidence.camera.distanceKm) ? event.evidence.camera.distanceKm : null,
        bearingDifference: Number.isFinite(event.evidence.camera.bearingDifference) ? event.evidence.camera.bearingDifference : null,
        viewQuality: event.evidence.camera.viewQuality || null,
        horizonSkyPct: Number.isFinite(event.evidence.camera.horizonSkyPct) ? event.evidence.camera.horizonSkyPct : null,
        nearestFrameOffsetMinutes: Number.isFinite(event.evidence.camera.nearestFrameOffsetMinutes) ? event.evidence.camera.nearestFrameOffsetMinutes : null,
        intervalMinutes: Number.isFinite(event.evidence.camera.intervalMinutes) ? event.evidence.camera.intervalMinutes : null,
      } : null,
      sunlightAssessmentAvailable: (event.researchAssessments || []).length > 0,
    })) : results ? reviewResults(events) : events;
    json(res, 200, { ok: true, events: items.length, counts, items });
    return;
  }
  if (req.method === "POST") {
    const body = await readJsonBody(req);
    if (!authorized(req, body)) { json(res, 401, { ok: false, error: "Unauthorized." }); return; }
    if (!REVIEW_LABELS.has(String(body.label || ""))) {
      json(res, 400, { ok: false, error: "Invalid label.", allowed: [...REVIEW_LABELS] }); return;
    }
    let event;
    try {
      event = await labelGoEvent(body.id, body);
    } catch (error) {
      if (error?.statusCode === 400) { json(res, 400, { ok: false, error: error.message }); return; }
      throw error;
    }
    if (!event) { json(res, 404, { ok: false, error: "Event not found." }); return; }
    json(res, 200, { ok: true, event });
    return;
  }
  res.setHeader("Allow", "GET, POST");
  json(res, 405, { ok: false, error: "Method not allowed." });
}
