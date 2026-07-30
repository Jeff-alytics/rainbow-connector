import { createHash } from "node:crypto";
import { configuredStore, distKm, redis, redisPipeline } from "./alert-common.mjs";
import { strictGoCandidates } from "./artifact-policy.mjs";

export const GO_EVENT_INDEX = "rainbow:go:events";
export const GO_EVENT_PREFIX = "rainbow:go:event:";
export const CONFIRMED_GALLERY_INDEX = "rainbow:gallery:confirmed";
export const CONFIRMED_GALLERY_PREFIX = "rainbow:gallery:item:";
export const REVIEW_ASSESSMENT_IDEMPOTENCY_PREFIX = "rainbow:review:assessment:";
const GO_SCAN_PREFIX = "rainbow:go:scan:";
const HISTORICAL_REVIEW_PREFIX = "archive-webcoos-";

export const REVIEW_LABELS = new Set([
  "pending", "rainbow", "no_rainbow", "possible", "obscured", "no_camera", "invalid_location",
]);

export const STRONG_POSSIBLE_MIN_SCORE = 50;
export const STRONG_POSSIBLE_MAX_PER_SCAN = 6;

function retentionDays() {
  const value = Number(process.env.GO_EVENT_RETENTION_DAYS || 90);
  return Math.max(7, Math.min(Number.isFinite(value) ? value : 90, 365));
}

function matchRadiusKm() {
  const value = Number(process.env.GO_EVENT_MATCH_RADIUS_KM || 40);
  return Math.max(10, Math.min(Number.isFinite(value) ? value : 40, 100));
}

function matchGapMinutes() {
  const value = Number(process.env.GO_EVENT_MATCH_GAP_MINUTES || 30);
  return Math.max(10, Math.min(Number.isFinite(value) ? value : 30, 120));
}

function finite(value) { return Number.isFinite(value) ? value : null; }

function compactGoes(goes) {
  if (!goes) return null;
  return {
    decision: goes.decision || null,
    positiveSources: Array.isArray(goes.positiveSources) ? goes.positiveSources : [],
    negativeSources: Array.isArray(goes.negativeSources) ? goes.negativeSources : [],
  };
}

function candidateClass(candidate) {
  return String(candidate?.verdict || "").toLowerCase() === "go" ? "GO" : "POSSIBLE";
}

export function reviewableCandidates(artifact, options = {}) {
  const go = strictGoCandidates(artifact).map(candidate => ({ ...candidate, verdict: "go" }));
  const possible = [...(artifact?.possibleCandidates || [])]
    .filter(candidate => {
      const score = Number(candidate?.evidence?.score);
      return Number.isFinite(score) && (options.includeAllPossibles
        || score >= STRONG_POSSIBLE_MIN_SCORE
        || candidate?.evidence?.selectionReason === "strong-radar-geometry-sunlight-uncertain");
    })
    .sort((a, b) => Number(b?.evidence?.score || 0) - Number(a?.evidence?.score || 0))
    .slice(0, options.includeAllPossibles ? 100 : STRONG_POSSIBLE_MAX_PER_SCAN)
    .map(candidate => ({ ...candidate, verdict: "watch" }));
  return [...go, ...possible];
}

export function compactGoDetection(candidate, generatedAt) {
  const evidence = candidate?.evidence || {};
  const persistence = candidate?.persistence || {};
  return {
    detectedAt: generatedAt,
    candidateId: candidate?.id || null,
    candidateClass: candidateClass(candidate),
    rank: finite(candidate?.rank),
    lat: finite(candidate?.lat),
    lon: finite(candidate?.lon),
    label: candidate?.label || candidate?.nearestZip?.name || null,
    nearestZip: candidate?.nearestZip || null,
    direction: candidate?.direction || null,
    score: finite(evidence.score),
    persistence: {
      confirmed: persistence.confirmed === true,
      scanCount: finite(persistence.scanCount),
      firstSeenAt: persistence.firstSeenAt || null,
      lastSeenAt: persistence.lastSeenAt || null,
    },
    evidence: {
      sunElevationDeg: finite(evidence.sunElevationDeg),
      rainbowArcDeg: finite(evidence.rainbowArcDeg),
      directNormalIrradianceWm2: finite(evidence.directNormalIrradianceWm2),
      directRadiationWm2: finite(evidence.directRadiationWm2),
      cloudCoverPct: finite(evidence.cloudCoverPct),
      rainPointDirectNormalIrradianceWm2: finite(evidence.rainPointDirectNormalIrradianceWm2),
      rainPointDirectRadiationWm2: finite(evidence.rainPointDirectRadiationWm2),
      rainPointCloudCoverPct: finite(evidence.rainPointCloudCoverPct),
      rainIntensity: finite(evidence.rainIntensity),
      observerRainIntensity: finite(evidence.observerRainIntensity),
      rainPoint: evidence.rainPoint || null,
      goes: compactGoes(evidence.goes),
    },
  };
}

function eventId(detection) {
  const seed = `${detection.detectedAt}|${detection.lat?.toFixed(3)}|${detection.lon?.toFixed(3)}`;
  return `go-${String(detection.detectedAt).replace(/[-:.TZ]/g, "").slice(0, 12)}-${createHash("sha256").update(seed).digest("hex").slice(0, 10)}`;
}

function historicalEventId(sourceKey) {
  const clean = String(sourceKey || "").trim();
  if (!clean) throw new Error("Historical review source key is required.");
  return HISTORICAL_REVIEW_PREFIX + createHash("sha256").update(clean).digest("hex").slice(0, 16);
}

export function newGoEvent(detection) {
  return {
    id: eventId(detection), firstSeenAt: detection.detectedAt, lastSeenAt: detection.detectedAt,
    scanCount: 1, detectionCount: 1, peakScore: detection.score, representative: detection,
    candidateClass: detection.candidateClass || "GO",
    candidateType: detection.candidateClass === "POSSIBLE" ? "live_possible" : "live_go",
    latestLocation: { lat: detection.lat, lon: detection.lon }, detections: [detection],
    review: { label: "pending", reviewedAt: null, notes: null, evidenceUrls: [] },
  };
}

export async function saveHistoricalReviewEvent(candidate) {
  if (!configuredStore()) return { stored: false, reason: "store_not_configured" };
  const observedAt = new Date(candidate?.observedAt || 0);
  const lat = Number(candidate?.lat), lon = Number(candidate?.lon);
  const bowBearing = Number(candidate?.bowBearing);
  if (!Number.isFinite(observedAt.getTime()) || !Number.isFinite(lat)
    || !Number.isFinite(lon) || !Number.isFinite(bowBearing)) {
    throw new Error("Historical review candidate has invalid time or geometry.");
  }
  const sourceKey = String(candidate?.sourceKey || `${candidate?.cameraId || "camera"}:${observedAt.toISOString()}`);
  const id = historicalEventId(sourceKey);
  const existing = await redis(["GET", GO_EVENT_PREFIX + id]);
  if (existing) return { stored: false, reason: "duplicate", event: JSON.parse(existing) };
  const detectedAt = observedAt.toISOString();
  const score = finite(Number(candidate?.score));
  const detection = {
    detectedAt, candidateId: id, rank: finite(Number(candidate?.rank)), lat, lon,
    label: candidate?.label || candidate?.cameraName || "Historical WebCOOS candidate",
    nearestZip: null,
    direction: { bearing: bowBearing, label: `Predicted bow direction ${Math.round(bowBearing)} degrees` },
    score,
    persistence: { confirmed: false, scanCount: 1, firstSeenAt: detectedAt, lastSeenAt: detectedAt },
    evidence: {
      sunElevationDeg: finite(Number(candidate?.sunElevationDeg)),
      rainbowArcDeg: finite(Number(candidate?.rainbowArcDeg)),
      directNormalIrradianceWm2: finite(Number(candidate?.directNormalIrradianceWm2)),
      directRadiationWm2: null,
      cloudCoverPct: finite(Number(candidate?.cloudCoverPct)),
      rainPointDirectNormalIrradianceWm2: null,
      rainPointDirectRadiationWm2: null,
      rainPointCloudCoverPct: null,
      rainIntensity: finite(Number(candidate?.rainIntensity)),
      observerRainIntensity: finite(Number(candidate?.observerRainIntensity)),
      rainPoint: candidate?.rainPoint || null,
      goes: null,
      archiveMethod: String(candidate?.archiveMethod || "historical weather + archived radar screen").slice(0, 180),
      radar: candidate?.radar || null,
    },
  };
  const queuedAt = new Date().toISOString();
  const event = {
    id, firstSeenAt: detectedAt, lastSeenAt: detectedAt, queuedAt,
    candidateType: "historical_webcoos", scanCount: 1, detectionCount: 1,
    peakScore: score, representative: detection,
    latestLocation: { lat, lon }, detections: [detection],
    review: { label: "pending", reviewedAt: null, notes: null, evidenceUrls: [] },
  };
  const ttlSeconds = retentionDays() * 24 * 60 * 60;
  await redisPipeline([
    ["SET", GO_EVENT_PREFIX + id, JSON.stringify(event), "EX", ttlSeconds],
    ["ZADD", GO_EVENT_INDEX, Date.now(), id],
  ]);
  return { stored: true, event };
}

export async function deleteHistoricalReviewEvent(id) {
  const cleanId = String(id || "").trim();
  if (!cleanId.startsWith(HISTORICAL_REVIEW_PREFIX)) return false;
  await redisPipeline([
    ["DEL", GO_EVENT_PREFIX + cleanId],
    ["ZREM", GO_EVENT_INDEX, cleanId],
  ]);
  return true;
}

function timeMs(value) {
  const parsed = new Date(value || 0).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

export function matchingEvent(events, detection, options = {}) {
  const radiusKm = options.radiusKm || matchRadiusKm();
  const gapMs = (options.gapMinutes || matchGapMinutes()) * 60 * 1000;
  const detectedMs = timeMs(detection.detectedAt);
  let best = null;
  for (const event of events) {
    const gap = detectedMs - timeMs(event.lastSeenAt);
    const point = event.latestLocation || event.representative;
    if (gap < 0 || gap > gapMs || !Number.isFinite(point?.lat) || !Number.isFinite(point?.lon)) continue;
    const distanceKm = distKm(detection.lat, detection.lon, point.lat, point.lon);
    if (distanceKm <= radiusKm && (!best || distanceKm < best.distanceKm)) best = { event, distanceKm };
  }
  return best?.event || null;
}

export function mergeDetection(event, detection) {
  const sameScan = event.lastSeenAt === detection.detectedAt;
  event.lastSeenAt = detection.detectedAt;
  if (!sameScan) event.scanCount = Number(event.scanCount || 0) + 1;
  event.detectionCount = Number(event.detectionCount || 0) + 1;
  event.latestLocation = { lat: detection.lat, lon: detection.lon };
  event.detections = [...(event.detections || []), detection].slice(-36);
  if (detection.candidateClass === "GO") {
    event.candidateClass = "GO";
    event.candidateType = "live_go";
  } else if (!event.candidateClass) {
    event.candidateClass = "POSSIBLE";
    event.candidateType = "live_possible";
  }
  const currentPeak = Number(event.peakScore);
  const nextScore = Number(detection.score);
  if (!Number.isFinite(currentPeak) || (Number.isFinite(nextScore) && nextScore > currentPeak)) {
    event.peakScore = Number.isFinite(nextScore) ? nextScore : event.peakScore;
    event.representative = detection;
  }
  return event;
}

async function loadEventsByIds(ids) {
  if (!Array.isArray(ids) || !ids.length) return [];
  const rows = await redis(["MGET", ...ids]);
  return (Array.isArray(rows) ? rows : []).map(row => {
    try { return row ? JSON.parse(row) : null; } catch { return null; }
  }).filter(Boolean);
}

export async function loadGoEvents(limit = 250) {
  if (!configuredStore()) return [];
  const count = Math.max(1, Math.min(Number(limit) || 250, 1000));
  const ids = await redis(["ZREVRANGE", GO_EVENT_INDEX, 0, count - 1]);
  return loadEventsByIds((ids || []).map(id => GO_EVENT_PREFIX + id));
}

export async function loadRecentGoEvents(sinceMs, limit = 50) {
  if (!configuredStore()) return [];
  const count = Math.max(1, Math.min(Number(limit) || 50, 250));
  const ids = await redis(["ZREVRANGEBYSCORE", GO_EVENT_INDEX, "+inf", Number(sinceMs) || 0, "LIMIT", 0, count]);
  return loadEventsByIds((ids || []).map(id => GO_EVENT_PREFIX + id));
}

export async function saveGoEvents(artifact, options = {}) {
  const generatedAt = artifact?.generatedAt || new Date().toISOString();
  const candidates = reviewableCandidates(artifact, options);
  if (!configuredStore()) return { stored: false, reason: "store_not_configured", candidates: candidates.length };
  const scanSeed = options.scanScope ? `${options.scanScope}|${generatedAt}` : generatedAt;
  const scanKey = GO_SCAN_PREFIX + createHash("sha256").update(scanSeed).digest("hex");
  const ttlSeconds = retentionDays() * 24 * 60 * 60;
  const claimed = await redis(["SET", scanKey, "1", "NX", "EX", ttlSeconds]);
  if (!claimed) return { stored: false, reason: "duplicate", candidates: candidates.length };
  try {
    const recent = await loadRecentGoEvents(timeMs(generatedAt) - matchGapMinutes() * 60 * 1000);
    const touched = new Map();
    let created = 0;
    for (const candidate of candidates) {
      const detection = compactGoDetection(candidate, generatedAt);
      let event = matchingEvent(recent, detection);
      if (event) mergeDetection(event, detection);
      else { event = newGoEvent(detection); recent.push(event); created++; }
      touched.set(event.id, event);
    }
    const commands = [];
    for (const event of touched.values()) {
      commands.push(["SET", GO_EVENT_PREFIX + event.id, JSON.stringify(event), "EX", ttlSeconds]);
      commands.push(["ZADD", GO_EVENT_INDEX, timeMs(event.lastSeenAt), event.id]);
    }
    commands.push(["ZREMRANGEBYSCORE", GO_EVENT_INDEX, "-inf", Date.now() - ttlSeconds * 1000]);
    await redisPipeline(commands);
    return { stored: true, candidates: candidates.length, eventsUpdated: touched.size, newEvents: created, eventIds: [...touched.keys()] };
  } catch (error) {
    await redis(["DEL", scanKey]).catch(() => {});
    throw error;
  }
}

export async function labelGoEvent(id, review) {
  const cleanId = String(id || "").trim();
  const label = String(review?.label || "").trim();
  if (!cleanId || !REVIEW_LABELS.has(label)) throw new Error("Invalid event or review label.");
  const raw = await redis(["GET", GO_EVENT_PREFIX + cleanId]);
  if (!raw) return null;
  const event = JSON.parse(raw);
  const eventFrames = event?.evidence?.frames || [];
  const requestedFrameUrls = [...new Set(
    (Array.isArray(review?.confirmedFrameUrls) ? review.confirmedFrameUrls : [review?.confirmedFrameUrl])
      .map(value => String(value || "").trim()).filter(Boolean).slice(0, 5)
  )];
  const confirmedFrames = requestedFrameUrls
    .map(url => eventFrames.find(frame => frame?.url === url))
    .filter(Boolean);
  if (label === "rainbow" && (!confirmedFrames.length || confirmedFrames.length !== requestedFrameUrls.length)) {
    const error = new Error("Choose every frame that shows the rainbow before saving.");
    error.statusCode = 400;
    throw error;
  }
  event.review = {
    label, reviewedAt: new Date().toISOString(),
    notes: String(review?.notes || "").trim().slice(0, 1000) || null,
    sceneSunlight: ["sunlit", "not_sunlit", "uncertain"].includes(review?.sceneSunlight) ? review.sceneSunlight : null,
    sunlightAssessmentExpandedBeforeGrade: review?.sunlightAssessmentExpandedBeforeGrade === true,
    evidenceUrls: Array.isArray(review?.evidenceUrls) ? review.evidenceUrls.map(value => String(value).trim()).filter(Boolean).slice(0, 10) : [],
    confirmedFrames: confirmedFrames.map(frame => ({
      url: frame.url,
      observedAt: frame.observedAt || null,
      source: frame.source || event.evidence?.source || null,
      cameraName: frame.cameraName || event.evidence?.camera?.name || null,
      distanceKm: finite(frame.distanceKm),
    })),
  };
  await redis(["SET", GO_EVENT_PREFIX + cleanId, JSON.stringify(event), "EX", retentionDays() * 24 * 60 * 60]);
  await syncConfirmedGallery(event);
  return event;
}

export function confirmedGalleryRecord(event) {
  const frames = (event?.review?.confirmedFrames || (event?.review?.confirmedFrame ? [event.review.confirmedFrame] : []))
    .map(frame => ({
      url: frame?.url,
      observedAt: frame?.observedAt || null,
      source: frame?.source || null,
      cameraName: frame?.cameraName || null,
      distanceKm: finite(frame?.distanceKm),
    }))
    .filter(frame => frame.url);
  if (event?.review?.label !== "rainbow" || !frames.length) return null;
  const rep = event.representative || {};
  return {
    id: event.id,
    confirmedAt: event.review.reviewedAt || new Date().toISOString(),
    detectedAt: rep.detectedAt || event.firstSeenAt || null,
    peakScore: Number.isFinite(event.peakScore) ? event.peakScore : null,
    scanCount: Number.isFinite(rep.persistence?.scanCount)
      ? rep.persistence.scanCount
      : Number.isFinite(event.scanCount) ? event.scanCount : null,
    camera: event.evidence?.camera || null,
    evidence: rep.evidence || null,
    frames,
  };
}

export async function syncConfirmedGallery(event) {
  const record = confirmedGalleryRecord(event);
  if (!event?.id) return { stored: false, reason: "missing_event" };
  if (!record) {
    await redisPipeline([
      ["DEL", CONFIRMED_GALLERY_PREFIX + event.id],
      ["ZREM", CONFIRMED_GALLERY_INDEX, event.id],
    ]);
    return { stored: false, reason: "not_confirmed_rainbow" };
  }
  const score = new Date(record.confirmedAt).getTime() || Date.now();
  await redisPipeline([
    ["SET", CONFIRMED_GALLERY_PREFIX + event.id, JSON.stringify(record)],
    ["ZADD", CONFIRMED_GALLERY_INDEX, score, event.id],
  ]);
  return { stored: true, eventId: event.id, frames: record.frames.length };
}

export async function loadConfirmedGallery(limit = 250) {
  const count = Math.max(1, Math.min(Number(limit) || 250, 1000));
  const ids = await redis(["ZREVRANGE", CONFIRMED_GALLERY_INDEX, 0, count - 1]);
  if (!Array.isArray(ids) || !ids.length) return [];
  const rows = await redis(["MGET", ...ids.map(id => CONFIRMED_GALLERY_PREFIX + id)]);
  return (Array.isArray(rows) ? rows : []).map(row => {
    try { return row ? JSON.parse(row) : null; } catch { return null; }
  }).filter(Boolean);
}

export async function attachGoEventEvidence(id, evidence) {
  const cleanId = String(id || "").trim();
  if (!cleanId) return null;
  const raw = await redis(["GET", GO_EVENT_PREFIX + cleanId]);
  if (!raw) return null;
  const event = JSON.parse(raw);
  event.evidence = {
    ...(event.evidence || {}),
    ...evidence,
    updatedAt: new Date().toISOString(),
  };
  await redis(["SET", GO_EVENT_PREFIX + cleanId, JSON.stringify(event), "EX", retentionDays() * 24 * 60 * 60]);
  return event;
}

export function matchingAssessmentEvent(events, assessment, options = {}) {
  const radiusKm = Number(options.radiusKm) || 3;
  const gapMs = (Number(options.gapMinutes) || 12) * 60 * 1000;
  const lat = Number(assessment?.observer?.lat), lon = Number(assessment?.observer?.lon);
  const observedMs = timeMs(assessment?.radarObservedAt);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || !observedMs) return null;
  let best = null;
  for (const event of events || []) {
    for (const detection of event.detections || [event.representative].filter(Boolean)) {
      const detectionMs = timeMs(detection?.detectedAt);
      if (!detectionMs || Math.abs(detectionMs - observedMs) > gapMs) continue;
      if (!Number.isFinite(detection?.lat) || !Number.isFinite(detection?.lon)) continue;
      const distanceKm = distKm(lat, lon, detection.lat, detection.lon);
      const timeGapMs = Math.abs(detectionMs - observedMs);
      if (distanceKm <= radiusKm && (!best || distanceKm < best.distanceKm
        || (distanceKm === best.distanceKm && timeGapMs < best.timeGapMs))) {
        best = { event, distanceKm, timeGapMs };
      }
    }
  }
  return best?.event || null;
}

export async function attachReviewAssessments(assessments, options = {}) {
  if (!configuredStore()) return { stored: false, reason: "store_not_configured", received: assessments?.length || 0 };
  const received = Array.isArray(assessments) ? assessments : [];
  if (!received.length) return { stored: true, received: 0, attached: 0, duplicates: 0, unmatched: 0 };
  const earliest = Math.min(...received.map(item => timeMs(item?.radarObservedAt)).filter(Boolean));
  const events = await loadRecentGoEvents(earliest - 20 * 60 * 1000, 250);
  const touched = new Map();
  let attached = 0, duplicates = 0, unmatched = 0;
  const ttlSeconds = retentionDays() * 24 * 60 * 60;
  for (const assessment of received) {
    const key = String(assessment?.idempotencyKey || "").trim();
    const candidateId = String(assessment?.candidateId || "").trim();
    if (!/^[a-f0-9]{64}$/.test(key) || !candidateId) { unmatched++; continue; }
    const event = matchingAssessmentEvent(events, assessment, options);
    if (!event) { unmatched++; continue; }
    const claimed = await redis(["SET", REVIEW_ASSESSMENT_IDEMPOTENCY_PREFIX + key, "1", "NX", "EX", ttlSeconds]);
    if (!claimed) { duplicates++; continue; }
    const compact = {
      ...assessment,
      receivedAt: new Date().toISOString(),
      matchedEventId: event.id,
    };
    event.researchAssessments = [...(event.researchAssessments || []).filter(item => item.idempotencyKey !== key), compact].slice(-24);
    touched.set(event.id, event);
    attached++;
  }
  const commands = [];
  for (const event of touched.values()) commands.push(["SET", GO_EVENT_PREFIX + event.id, JSON.stringify(event), "EX", ttlSeconds]);
  await redisPipeline(commands);
  return { stored: true, received: received.length, attached, duplicates, unmatched, eventIds: [...touched.keys()] };
}
