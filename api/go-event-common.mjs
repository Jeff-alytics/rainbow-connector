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
const EVENT_CAS_RETRIES = 8;
const EVENT_CAS_LUA = `
local current = redis.call('GET', KEYS[1])
if not current or redis.sha1hex(current) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
`;

export const REVIEW_LABELS = new Set([
  "pending", "rainbow", "no_rainbow", "possible", "obscured", "no_camera", "invalid_location",
]);

export const STRONG_POSSIBLE_MIN_SCORE = 50;
export const STRONG_POSSIBLE_MAX_PER_SCAN = 6;

function retentionDays() {
  const value = Number(process.env.GO_EVENT_RETENTION_DAYS || 90);
  return Math.max(7, Math.min(Number.isFinite(value) ? value : 90, 365));
}

function eventTtlSeconds() {
  return retentionDays() * 24 * 60 * 60;
}

export async function mutateGoEvent(id, mutator, retries = EVENT_CAS_RETRIES) {
  const cleanId = String(id || "").trim();
  if (!cleanId) return null;
  const storageKey = GO_EVENT_PREFIX + cleanId;
  for (let attempt = 0; attempt < retries; attempt++) {
    const raw = await redis(["GET", storageKey]);
    if (!raw) return null;
    const event = JSON.parse(raw);
    const next = mutator(event);
    if (!next) return null;
    const stored = await casReplaceGoEvent(cleanId, raw, next);
    if (Number(stored) === 1) return next;
  }
  throw new Error(`Concurrent event update did not converge for ${cleanId}`);
}

async function casReplaceGoEvent(id, raw, next) {
  const expected = createHash("sha1").update(raw).digest("hex");
  return redis(["EVAL", EVENT_CAS_LUA, 1, GO_EVENT_PREFIX + id, expected,
    JSON.stringify(next), eventTtlSeconds()]);
}

async function createGoEvent(event) {
  const stored = await redis(["SET", GO_EVENT_PREFIX + event.id, JSON.stringify(event), "NX", "EX", eventTtlSeconds()]);
  return stored ? event : null;
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
    if (!options.includeResearch && event?.candidateType === "research_possible") continue;
    const gap = detectedMs - timeMs(event.lastSeenAt);
    const point = event.latestLocation || event.representative;
    if (gap < 0 || gap > gapMs || !Number.isFinite(point?.lat) || !Number.isFinite(point?.lon)) continue;
    const distanceKm = distKm(detection.lat, detection.lon, point.lat, point.lon);
    if (distanceKm <= radiusKm && (!best || distanceKm < best.distanceKm)) best = { event, distanceKm };
  }
  return best?.event || null;
}

export function mergeDetection(event, detection) {
  const researchDetection = ["geometry-first-review-only", "opportunity-ledger-camera-gated-review-only"]
    .includes(detection?.evidence?.selectionReason);
  if ((event?.candidateType === "research_possible") !== researchDetection) {
    throw new Error("Operational and research detections cannot share an event");
  }
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
    const recent = (await loadRecentGoEvents(timeMs(generatedAt) - matchGapMinutes() * 60 * 1000))
      .filter(event => event.candidateType !== "research_possible");
    const touched = new Set();
    let created = 0;
    for (const candidate of candidates) {
      const detection = compactGoDetection(candidate, generatedAt);
      let event = matchingEvent(recent, detection);
      if (event) {
        event = await mutateGoEvent(event.id, current => {
          if (current.candidateType === "research_possible") throw new Error("Research event rejected from operational merge");
          return mergeDetection(current, detection);
        });
        if (!event) throw new Error("Operational event disappeared during merge");
        const index = recent.findIndex(item => item.id === event.id);
        if (index >= 0) recent[index] = event;
      } else {
        event = newGoEvent(detection);
        const inserted = await createGoEvent(event);
        if (!inserted) {
          const collision = (await loadEventsByIds([GO_EVENT_PREFIX + event.id]))[0];
          if (!collision || collision.candidateType === "research_possible") throw new Error("Operational event id collision");
          event = await mutateGoEvent(collision.id, current => mergeDetection(current, detection));
          if (!event) throw new Error("Colliding operational event disappeared during merge");
        } else {
          created++;
        }
        recent.push(event);
      }
      touched.add(event.id);
    }
    const commands = [];
    for (const id of touched) {
      const event = recent.find(item => item.id === id);
      commands.push(["ZADD", GO_EVENT_INDEX, timeMs(event?.lastSeenAt), id]);
    }
    commands.push(["ZREMRANGEBYSCORE", GO_EVENT_INDEX, "-inf", Date.now() - ttlSeconds * 1000]);
    await redisPipeline(commands);
    return { stored: true, candidates: candidates.length, eventsUpdated: touched.size, newEvents: created, eventIds: [...touched] };
  } catch (error) {
    await redis(["DEL", scanKey]).catch(() => {});
    throw error;
  }
}

export async function labelGoEvent(id, review, attempt = 0) {
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
  if (Number(await casReplaceGoEvent(cleanId, raw, event)) !== 1) {
    if (attempt + 1 >= EVENT_CAS_RETRIES) throw new Error(`Concurrent review update did not converge for ${cleanId}`);
    return labelGoEvent(cleanId, review, attempt + 1);
  }
  await syncConfirmedGallery(event);
  return event;
}

export function evidenceFrameReviewKey(frame) {
  const source = encodeURIComponent(String(frame.source || "camera"));
  if (frame?.cameraId != null) {
    const site = encodeURIComponent(String(frame.siteId ?? "site"));
    const camera = encodeURIComponent(String(frame.cameraId));
    return `${source}:${site}:${camera}`;
  }
  const cameraName = String(frame?.cameraName || "").trim();
  return cameraName ? `${source}:name:${encodeURIComponent(cameraName)}` : null;
}

export function evidenceFrameReviewGroups(event) {
  const groups = new Map();
  for (const frame of event?.evidence?.frames || []) {
    const key = evidenceFrameReviewKey(frame);
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, { key, frames: [] });
    groups.get(key).frames.push(frame);
  }
  return [...groups.values()];
}

export async function labelGoEventView(id, cameraKey, review, attempt = 0) {
  const cleanId = String(id || "").trim();
  const cleanCameraKey = String(cameraKey || "").trim();
  const label = String(review?.label || "").trim();
  if (!cleanId || !cleanCameraKey || !REVIEW_LABELS.has(label)) throw new Error("Invalid event, camera, or review label.");
  const raw = await redis(["GET", GO_EVENT_PREFIX + cleanId]);
  if (!raw) return null;
  const event = JSON.parse(raw);
  const group = evidenceFrameReviewGroups(event).find(item => item.key === cleanCameraKey);
  if (!group) {
    const error = new Error("That camera view is no longer available for review.");
    error.statusCode = 400;
    throw error;
  }
  const requestedFrameUrls = [...new Set(
    (Array.isArray(review?.confirmedFrameUrls) ? review.confirmedFrameUrls : [review?.confirmedFrameUrl])
      .map(value => String(value || "").trim()).filter(Boolean).slice(0, 5)
  )];
  const confirmedFrames = requestedFrameUrls
    .map(url => group.frames.find(frame => frame?.url === url))
    .filter(Boolean);
  if (label === "rainbow" && (!confirmedFrames.length || confirmedFrames.length !== requestedFrameUrls.length)) {
    const error = new Error("Choose every frame that shows the rainbow before saving.");
    error.statusCode = 400;
    throw error;
  }
  event.viewReviews = { ...(event.viewReviews || {}) };
  event.viewReviews[cleanCameraKey] = {
    label, reviewedAt: new Date().toISOString(), cameraKey: cleanCameraKey,
    notes: String(review?.notes || "").trim().slice(0, 1000) || null,
    sceneSunlight: ["sunlit", "not_sunlit", "uncertain"].includes(review?.sceneSunlight) ? review.sceneSunlight : null,
    sunlightAssessmentExpandedBeforeGrade: review?.sunlightAssessmentExpandedBeforeGrade === true,
    evidenceUrls: Array.isArray(review?.evidenceUrls) ? review.evidenceUrls.map(value => String(value).trim()).filter(Boolean).slice(0, 10) : [],
    confirmedFrames: confirmedFrames.map(frame => ({
      url: frame.url, observedAt: frame.observedAt || null,
      source: frame.source || event.evidence?.source || null,
      cameraName: frame.cameraName || null, siteId: frame.siteId ?? null, cameraId: frame.cameraId ?? null,
      distanceKm: finite(frame.distanceKm),
    })),
  };
  if (Number(await casReplaceGoEvent(cleanId, raw, event)) !== 1) {
    if (attempt + 1 >= EVENT_CAS_RETRIES) throw new Error(`Concurrent view review update did not converge for ${cleanId}`);
    return labelGoEventView(cleanId, cleanCameraKey, review, attempt + 1);
  }
  await syncConfirmedGallery(event);
  return event;
}

export function confirmedGalleryRecord(event) {
  const globalFrames = event?.review?.confirmedFrames || (event?.review?.confirmedFrame ? [event.review.confirmedFrame] : []);
  const rainbowViewReviews = Object.values(event?.viewReviews || {}).filter(review => review?.label === "rainbow");
  const allFrames = [
    ...(event?.review?.label === "rainbow" ? globalFrames : []),
    ...rainbowViewReviews.flatMap(review => review.confirmedFrames || []),
  ];
  const frames = allFrames
    .map(frame => ({
      url: frame?.url,
      observedAt: frame?.observedAt || null,
      source: frame?.source || null,
      cameraName: frame?.cameraName || null,
      distanceKm: finite(frame?.distanceKm),
    }))
    .filter((frame, index, items) => frame.url && items.findIndex(item => item.url === frame.url) === index);
  if (!frames.length) return null;
  const rep = event.representative || {};
  const reviewedTimes = [event?.review?.label === "rainbow" ? event.review.reviewedAt : null,
    ...rainbowViewReviews.map(review => review.reviewedAt)].filter(Boolean);
  return {
    id: event.id,
    confirmedAt: reviewedTimes.sort().at(-1) || new Date().toISOString(),
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
  return mutateGoEvent(cleanId, event => {
    event.evidence = {
      ...(event.evidence || {}),
      ...evidence,
      updatedAt: new Date().toISOString(),
    };
    return event;
  });
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

export function researchDetectionFromAssessment(assessment) {
  const observer = assessment?.observer || {}, rain = assessment?.rain || {};
  const geometry = assessment?.geometry || {}, at = assessment?.radarObservedAt;
  const lat = Number(observer.lat), lon = Number(observer.lon), detectedMs = timeMs(at);
  if (!Number.isFinite(lat) || !Number.isFinite(lon) || !detectedMs) return null;
  const elevation = Number(geometry.sunElevationDeg), antiSolar = Number(geometry.antiSolarBearingDeg);
  const apparentElevation = Number(geometry.apparentSunElevationDeg);
  const score = Number(geometry.radarScore), rainRate = Number(rain.rateMmHr);
  const observerRain = Number(rain.observerRateMmHr), rainDistance = Number(rain.distanceKm);
  const ledger = assessment?.researchReview?.source === "opportunity_ledger";
  return {
    detectedAt: new Date(detectedMs).toISOString(), candidateId: assessment.candidateId || null,
    candidateClass: "POSSIBLE", rank: finite(assessment?.researchReview?.rankWithinScan), lat, lon,
    label: ledger ? "Opportunity-ledger research candidate" : "Geometry-first research candidate", nearestZip: null,
    direction: Number.isFinite(antiSolar) ? { bearing: antiSolar, label: `Predicted bow direction ${Math.round(antiSolar)} degrees` } : null,
    score: finite(score), persistence: { confirmed: false, scanCount: 1, firstSeenAt: at, lastSeenAt: at },
    evidence: {
      sunElevationDeg: finite(elevation),
      apparentSunElevationDeg: finite(apparentElevation),
      rainbowArcDeg: Number.isFinite(apparentElevation) ? Math.max(0, 42 - apparentElevation)
        : Number.isFinite(elevation) ? Math.max(0, 42 - elevation) : null,
      directNormalIrradianceWm2: null, directRadiationWm2: null, cloudCoverPct: null,
      rainPointDirectNormalIrradianceWm2: null, rainPointDirectRadiationWm2: null,
      rainPointCloudCoverPct: null, rainIntensity: finite(rainRate), observerRainIntensity: finite(observerRain),
      rainPoint: Number.isFinite(Number(rain.lat)) && Number.isFinite(Number(rain.lon))
        ? { lat: Number(rain.lat), lon: Number(rain.lon), distanceKm: finite(rainDistance), bearing: null } : null,
      goes: null, selectionReason: ledger ? "opportunity-ledger-camera-gated-review-only" : "geometry-first-review-only",
      researchRuleVersion: assessment?.researchReview?.ruleVersion || null,
      researchSource: assessment?.researchReview?.source || "detector_rejection_log",
      currentDetectorDisposition: assessment?.researchReview?.currentDetectorDisposition || assessment?.disposition || null,
      antiSolarRainArcSpanDeg: finite(rain.antiSolarRainArcSpanDeg),
      antiSolarRainSpanByTier: rain.antiSolarRainSpanByTier || null,
    },
  };
}

export function newResearchReviewEvent(assessment) {
  const detection = researchDetectionFromAssessment(assessment);
  if (!detection) return null;
  const event = newGoEvent(detection);
  event.candidateClass = "POSSIBLE";
  event.candidateType = "research_possible";
  event.researchRuleVersion = assessment?.researchReview?.ruleVersion || null;
  event.researchSource = assessment?.researchReview?.source || "detector_rejection_log";
  event.ledgerEventId = assessment?.researchReview?.ledgerEventId || null;
  return event;
}

export async function attachReviewAssessments(assessments, options = {}) {
  if (!configuredStore()) return { stored: false, reason: "store_not_configured", received: assessments?.length || 0 };
  const received = Array.isArray(assessments) ? assessments : [];
  if (!received.length) return { stored: true, received: 0, attached: 0, duplicates: 0, unmatched: 0 };
  const earliest = Math.min(...received.map(item => timeMs(item?.radarObservedAt)).filter(Boolean));
  const events = await loadRecentGoEvents(earliest - 20 * 60 * 1000, 250);
  let attached = 0, duplicates = 0, unmatched = 0, created = 0;
  const errors = [];
  const ttlSeconds = retentionDays() * 24 * 60 * 60;
  const eventIds = new Set();
  for (const assessment of received) {
    try {
    const key = String(assessment?.idempotencyKey || "").trim();
    const candidateId = String(assessment?.candidateId || "").trim();
    if (!/^[a-f0-9]{64}$/.test(key) || !candidateId) { unmatched++; continue; }
    if (await redis(["GET", REVIEW_ASSESSMENT_IDEMPOTENCY_PREFIX + key])) { duplicates++; continue; }
    const research = assessment?.disposition === "selected_research_possible";
    const pool = events.filter(event => research
      ? event.candidateType === "research_possible"
      : event.candidateType !== "research_possible");
    const ledgerEventId = String(assessment?.researchReview?.ledgerEventId || "").trim();
    let event = research && ledgerEventId
      ? pool.find(item => item.ledgerEventId === ledgerEventId
        && Math.abs(timeMs(item.lastSeenAt) - timeMs(assessment.radarObservedAt)) <= (options.researchGapMinutes || 12) * 60 * 1000)
      : null;
    event ||= matchingAssessmentEvent(pool, assessment, research
      ? { ...options, radiusKm: options.researchRadiusKm || 35, gapMinutes: options.researchGapMinutes || 12 }
      : { ...options, radiusKm: options.radiusKm || 3 });
    let isNew = false;
    if (!event && research) {
      event = newResearchReviewEvent(assessment);
      if (event) {
        const inserted = await createGoEvent(event);
        if (!inserted) {
          event = (await loadEventsByIds([GO_EVENT_PREFIX + event.id]))[0] || null;
        } else {
          events.push(event); isNew = true; created++;
        }
      }
    }
    if (!event) { unmatched++; continue; }
    const targetId = event.id;
    event = await mutateGoEvent(targetId, current => {
      if ((current.candidateType === "research_possible") !== research) {
        throw new Error("Research assessment event class changed during update");
      }
      if (research && !isNew) {
        const detection = researchDetectionFromAssessment(assessment);
        if (detection && !(current.detections || []).some(item => item.detectedAt === detection.detectedAt)) {
          mergeDetection(current, detection);
        }
      }
      if (assessment?.researchReview?.source) current.researchSource = assessment.researchReview.source;
      if (assessment?.researchReview?.ledgerEventId) current.ledgerEventId = assessment.researchReview.ledgerEventId;
      const compact = { ...assessment, receivedAt: new Date().toISOString(), matchedEventId: current.id };
      current.researchAssessments = [
        ...(current.researchAssessments || []).filter(item => item.idempotencyKey !== key), compact,
      ].slice(-24);
      return current;
    });
    if (!event) { unmatched++; continue; }
    const index = events.findIndex(item => item.id === event.id);
    if (index >= 0) events[index] = event; else events.push(event);
    const claimed = await redis(["SET", REVIEW_ASSESSMENT_IDEMPOTENCY_PREFIX + key, "1", "NX", "EX", ttlSeconds]);
    if (!claimed) duplicates++; else attached++;
    await redis(["ZADD", GO_EVENT_INDEX, timeMs(event.lastSeenAt), event.id]);
    eventIds.add(event.id);
    } catch (error) {
      unmatched++;
      errors.push({ candidateId: String(assessment?.candidateId || "").slice(0, 120), error: String(error?.message || error).slice(0, 300) });
      console.warn("[review-assessment] record failed:", error?.message || error);
    }
  }
  return { stored: true, received: received.length, attached, duplicates, unmatched, created,
    errors, eventIds: [...eventIds] };
}
