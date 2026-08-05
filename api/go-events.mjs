import { json, readJsonBody, verifySecret } from "./alert-common.mjs";
import { REVIEW_LABELS, evidenceFrameReviewGroups, labelGoEvent, labelGoEventView, loadGoEvents,
  loadGoEventsBetween, loadGoEventsBySource, loadReviewedGoEvents } from "./go-event-common.mjs";
import { hasReviewSession } from "./review-auth-common.mjs";

import { activeCameraExclusionMap, appendCameraExclusion, loadCameraExclusionRegistry } from './review-camera-exclusions.mjs';

export const config = { maxDuration: 10 };

export const TRUSTED_REVIEW_SOURCE_PREFIXES = Object.freeze([
  'faa weathercam',
  'webcoos',
  'usgs nims',
  'alertcalifornia',
  'alertwest',
  'new york state mesonet',
]);

export function trustedReviewSource(value) {
  const source = String(value || '').trim().toLowerCase();
  return TRUSTED_REVIEW_SOURCE_PREFIXES.some(prefix => source.startsWith(prefix));
}

function groupSource(event, group) {
  return group?.frames?.find(frame => frame?.source)?.source || event?.evidence?.source || null;
}

function pendingReviewGroup(event, group) {
  return (event?.viewReviews?.[group.key]?.label || 'pending') === 'pending';
}

function allowedReviewGroup(event, group, excludedCameraKeys) {
  return trustedReviewSource(groupSource(event, group)) && !excludedCameraKeys.has(group.key);
}

export function reviewWithholdingSummary(events, excludedCameraKeys = new Set()) {
  const summary = { disallowedSourceViews: 0, excludedCameraViews: 0 };
  for (const event of events || []) {
    const groups = evidenceFrameReviewGroups(event);
    if (!groups.length && (event.evidence?.frames || []).length
      && (event.review?.label || 'pending') === 'pending'
      && !trustedReviewSource(event.evidence?.source)) {
      summary.disallowedSourceViews++;
    }
    for (const group of groups) {
      if (!pendingReviewGroup(event, group)) continue;
      if (!trustedReviewSource(groupSource(event, group))) summary.disallowedSourceViews++;
      else if (excludedCameraKeys.has(group.key)) summary.excludedCameraViews++;
    }
  }
  return summary;
}

export function apparentSolarElevationDeg(geometricElevationDeg) {
  const elevation = Number(geometricElevationDeg);
  if (!Number.isFinite(elevation)) return null;
  if (elevation > 85) return elevation;
  const tangent = Math.tan(elevation * Math.PI / 180);
  let correctionArcSeconds;
  if (elevation > 5) {
    correctionArcSeconds = 58.1 / tangent - 0.07 / tangent ** 3 + 0.000086 / tangent ** 5;
  } else if (elevation > -0.575) {
    correctionArcSeconds = 1735 + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)));
  } else {
    correctionArcSeconds = -20.774 / tangent;
  }
  return elevation + correctionArcSeconds / 3600;
}

function authorized(req, body = {}) {
  return hasReviewSession(req) || verifySecret(req, body);
}

export function reviewQueue(events, options = {}) {
  const excludedCameraKeys = options.excludedCameraKeys || new Set();
  const strengthRank = { strong: 3, usable: 2, limited: 1, unknown: 0 };
  const ordered = [...(events || [])]
    .filter(event => {
      const groups = evidenceFrameReviewGroups(event);
      if (groups.length && ((event.review?.label || "pending") === "pending" || Object.keys(event.viewReviews || {}).length)) {
        return groups.some(group => (event.viewReviews?.[group.key]?.label || "pending") === "pending");
      }
      return (event.review?.label || "pending") === "pending";
    })
    .filter(event => (event.evidence?.frames || []).length > 0)
    .filter(event => {
      const groups = evidenceFrameReviewGroups(event);
      return groups.length
        ? groups.some(group => pendingReviewGroup(event, group)
          && allowedReviewGroup(event, group, excludedCameraKeys))
        : trustedReviewSource(event.evidence?.source);
    })
    .sort((a, b) => {
      const aClass = eventClass(a) === "GO" ? 1 : 0;
      const bClass = eventClass(b) === "GO" ? 1 : 0;
      const aCamera = a.evidence?.camera || {}, bCamera = b.evidence?.camera || {};
      const aStrength = reviewEvidenceStrength(aCamera.distanceKm, aCamera.bearingDifference, aCamera.viewQuality, aCamera.nearestFrameOffsetMinutes, aCamera.visibleBowFraction);
      const bStrength = reviewEvidenceStrength(bCamera.distanceKm, bCamera.bearingDifference, bCamera.viewQuality, bCamera.nearestFrameOffsetMinutes, bCamera.visibleBowFraction);
      return bClass - aClass
      || strengthRank[bStrength] - strengthRank[aStrength]
      || Number(b.peakScore || 0) - Number(a.peakScore || 0)
      || new Date(b.lastSeenAt || 0) - new Date(a.lastSeenAt || 0);
    });
  return ordered;
}

export function allCameraViewsReviewed(event, options = {}) {
  const excludedCameraKeys = options.excludedCameraKeys || new Set();
  const allGroups = evidenceFrameReviewGroups(event);
  const groups = allGroups.filter(group => allowedReviewGroup(event, group, excludedCameraKeys));
  if (!groups.length) return allGroups.length > 0
    || Boolean(event?.review?.label && event.review.label !== "pending");
  return groups.every(group => {
    const label = event?.viewReviews?.[group.key]?.label;
    return Boolean(label && label !== "pending");
  });
}

export function reviewSafeEvent(event, options = {}) {
  if (allCameraViewsReviewed(event, options)) return event;
  const { researchAssessments: _hidden, ...safe } = event || {};
  return safe;
}

function cameraForGroup(event, group) {
  const first = group?.frames?.[0] || {};
  const stored = (event?.evidence?.cameras || []).find(camera =>
    String(camera?.siteId) === String(first.siteId) && String(camera?.cameraId) === String(first.cameraId));
  return {
    name: first.cameraName || stored?.name || event?.evidence?.camera?.name || null,
    direction: stored?.direction || null,
    source: first.source || event?.evidence?.source || null,
    distanceKm: Number.isFinite(first.distanceKm) ? first.distanceKm : Number.isFinite(stored?.distanceKm) ? stored.distanceKm : null,
    bearingDifference: Number.isFinite(first.bearingDifference) ? first.bearingDifference : Number.isFinite(stored?.bearingDifference) ? stored.bearingDifference : null,
    viewQuality: first.viewQuality || stored?.viewQuality || null,
    horizonSkyPct: Number.isFinite(stored?.horizonSkyPct) ? stored.horizonSkyPct : null,
    nearestFrameOffsetMinutes: group.frames.reduce((best, frame) => {
      const value = Math.abs(Number(frame.timeOffsetMinutes));
      return Number.isFinite(value) && (!Number.isFinite(best) || value < best) ? value : best;
    }, null),
    intervalMinutes: Number.isFinite(stored?.intervalMinutes) ? stored.intervalMinutes : null,
    visibleBowFraction: Number.isFinite(first.visibleBowFraction) ? first.visibleBowFraction : Number.isFinite(stored?.visibleBowFraction) ? stored.visibleBowFraction : null,
    bowArcOverlapDeg: Number.isFinite(first.bowArcOverlapDeg) ? first.bowArcOverlapDeg : Number.isFinite(stored?.bowArcOverlapDeg) ? stored.bowArcOverlapDeg : null,
  };
}

function queueItem(event, group = null, options = {}) {
  const frames = group?.frames || event.evidence?.frames || [];
  const geometricSunElevationDeg = Number(event?.representative?.evidence?.sunElevationDeg);
  const apparentSunElevationDeg = apparentSolarElevationDeg(geometricSunElevationDeg);
  const camera = group ? cameraForGroup(event, group) : event.evidence?.camera ? {
    name: event.evidence.camera.name || null,
    direction: event.evidence.camera.direction || null,
    source: event.evidence.source || null,
    distanceKm: Number.isFinite(event.evidence.camera.distanceKm) ? event.evidence.camera.distanceKm : null,
    bearingDifference: Number.isFinite(event.evidence.camera.bearingDifference) ? event.evidence.camera.bearingDifference : null,
    viewQuality: event.evidence.camera.viewQuality || null,
    horizonSkyPct: Number.isFinite(event.evidence.camera.horizonSkyPct) ? event.evidence.camera.horizonSkyPct : null,
    nearestFrameOffsetMinutes: Number.isFinite(event.evidence.camera.nearestFrameOffsetMinutes) ? event.evidence.camera.nearestFrameOffsetMinutes : null,
    intervalMinutes: Number.isFinite(event.evidence.camera.intervalMinutes) ? event.evidence.camera.intervalMinutes : null,
    visibleBowFraction: Number.isFinite(event.evidence.camera.visibleBowFraction) ? event.evidence.camera.visibleBowFraction : null,
    bowArcOverlapDeg: Number.isFinite(event.evidence.camera.bowArcOverlapDeg) ? event.evidence.camera.bowArcOverlapDeg : null,
  } : null;
  const latestAssessment = (event.researchAssessments || []).at(-1) || null;
  return {
    id: group ? `${event.id}::${group.key}` : event.id,
    eventId: event.id,
    cameraKey: group?.key || null,
    candidateType: event.candidateType || "live_go",
    researchSource: event.researchSource || null,
    scanCount: Number.isFinite(event.scanCount) ? event.scanCount : null,
    // Detailed selection evidence can anchor the human grade just as strongly
    // as the sunlight verdict. Reveal it only after every sibling camera view
    // has been graded.
    researchContext: event.candidateType === "research_possible" && allCameraViewsReviewed(event, options) ? {
      selectionReason: latestAssessment?.researchReview?.selectionReason || event.representative?.evidence?.selectionReason || null,
      currentDetectorDisposition: latestAssessment?.researchReview?.currentDetectorDisposition
        || event.representative?.evidence?.currentDetectorDisposition || null,
      persistenceScans: latestAssessment?.researchReview?.persistenceScans || event.scanCount || null,
      rainArcSpanDeg: latestAssessment?.rain?.antiSolarRainArcSpanDeg
        ?? event.representative?.evidence?.antiSolarRainArcSpanDeg ?? null,
      ruleVersion: latestAssessment?.researchReview?.ruleVersion || event.researchRuleVersion || null,
    } : null,
    candidateClass: eventClass(event),
    detectedAt: event.representative?.detectedAt || event.firstSeenAt || null,
    sunElevationDeg: Number.isFinite(geometricSunElevationDeg) ? geometricSunElevationDeg : null,
    apparentSunElevationDeg,
    expectedBowTopDeg: Number.isFinite(apparentSunElevationDeg) ? Math.max(0, 42 - apparentSunElevationDeg) : null,
    frames: frames.map(frame => ({
      url: frame.url, observedAt: frame.observedAt || null, source: frame.source || event.evidence?.source || null,
      cameraName: frame.cameraName || null, siteId: frame.siteId ?? null, cameraId: frame.cameraId ?? null,
      distanceKm: Number.isFinite(frame.distanceKm) ? frame.distanceKm : null,
      viewQuality: frame.viewQuality || null,
      timeOffsetMinutes: Number.isFinite(frame.timeOffsetMinutes) ? frame.timeOffsetMinutes : null,
      visibleBowFraction: Number.isFinite(frame.visibleBowFraction) ? frame.visibleBowFraction : null,
      bowArcOverlapDeg: Number.isFinite(frame.bowArcOverlapDeg) ? frame.bowArcOverlapDeg : null,
      negativeEvidenceEligible: frame.negativeEvidenceEligible === true,
    })),
    camera,
  };
}

export function reviewQueueItems(events, options = {}) {
  const excludedCameraKeys = options.excludedCameraKeys || new Set();
  const items = reviewQueue(events, options).flatMap(event => {
    const allGroups = evidenceFrameReviewGroups(event);
    if (!allGroups.length) {
      const item = queueItem(event, null, options);
      return Number.isFinite(item.camera?.distanceKm) && item.camera.distanceKm > 40 ? [] : [item];
    }
    const groups = allGroups.filter(group => {
      const distance = Number(group.frames?.[0]?.distanceKm);
      return allowedReviewGroup(event, group, excludedCameraKeys)
        && (!Number.isFinite(distance) || distance <= 40);
    });
    return groups
      .filter(group => (event.viewReviews?.[group.key]?.label || "pending") === "pending")
      .map(group => queueItem(event, group, options));
  });
  const operational = items.filter(item => item.candidateType !== "research_possible");
  const researchItems = items.filter(item => item.candidateType === "research_possible");
  return [...operational, ...researchItems];
}

export function v4ShadowDaily(events) {
  const rows = [];
  for (const event of events || []) {
    if (event.researchSource !== "v4_shadow") continue;
    const hasEvidenceFrames = (event.evidence?.frames || []).length > 0;
    const awaitingCameraGrade = hasEvidenceFrames && !allCameraViewsReviewed(event);
    for (const prediction of event.v4ShadowPredictions || []) {
      if (!["primary", "go", "possible"].includes(prediction.lane) || !prediction.detectedAt) continue;
      rows.push({
        eventId: event.id, predictionId: prediction.predictionId,
        predictionSha256: prediction.predictionSha256 || null,
        mechanicalFreezeContentSha256: prediction.mechanicalFreezeContentSha256 || null,
        detectedAt: prediction.detectedAt, lat: prediction.lat, lon: prediction.lon,
        bowBearingDeg: prediction.bowBearingDeg,
        score: awaitingCameraGrade ? null : prediction.score,
        rankWithinScan: awaitingCameraGrade ? null : prediction.rankWithinScan,
        poolSize: awaitingCameraGrade ? null : prediction.poolSize,
        lane: prediction.lane, classification: prediction.classification || null,
        sunlightState: prediction.sunlightState || null, modelVersion: prediction.modelVersion,
        ruleVersion: prediction.ruleVersion, familyEventId: prediction.familyEventId,
        hasMatchedCamera: prediction.hasMatchedCamera === true,
        hasCameraEvidence: hasEvidenceFrames,
        cameraMatches: prediction.cameraMatches || [],
        evidenceCamera: event.evidence?.camera || null,
        reviewLabel: event.review?.label || "pending",
        cameraReviewLabels: Object.values(event.viewReviews || {}).map(review => review?.label).filter(Boolean),
        modelDetailsWithheld: awaitingCameraGrade,
      });
    }
  }
  rows.sort((a, b) => new Date(b.detectedAt) - new Date(a.detectedAt)
    || Number(a.rankWithinScan || 999) - Number(b.rankWithinScan || 999));
  const grouped = new Map();
  for (const row of rows) {
    const day = String(row.detectedAt).slice(0, 10);
    if (!grouped.has(day)) grouped.set(day, new Map());
    const families = grouped.get(day);
    const key = row.familyEventId || row.predictionId;
    const existing = families.get(key);
    if (existing) existing.scanSelections++;
    else families.set(key, { ...row, scanSelections: 1 });
  }
  return [...grouped].map(([date, families]) => ({ date, items: [...families.values()] }));
}

function eventClass(event) {
  if (event?.candidateClass === "POSSIBLE" || event?.candidateType === "live_possible") return "POSSIBLE";
  if (String(event?.candidateType || "").startsWith("historical_")) return "ARCHIVE";
  return "GO";
}

export function reviewEvidenceStrength(distanceKm, bearingDifference, viewQuality = "usable", frameTimeErrorMinutes = null, visibleBowFraction = null) {
  if (!Number.isFinite(distanceKm) || !Number.isFinite(bearingDifference)) return "unknown";
  if (["limited", "unreviewed"].includes(viewQuality)
    || (Number.isFinite(visibleBowFraction) && visibleBowFraction < 0.5)
    || (Number.isFinite(frameTimeErrorMinutes) && frameTimeErrorMinutes > 8)) return "limited";
  if (distanceKm <= 25 && bearingDifference <= 15) return "strong";
  if (distanceKm <= 35 && bearingDifference <= 30) return "usable";
  return "limited";
}

export function reviewResults(events, options = {}) {
  // An event still in the queue has a view awaiting an unbiased grade, and this
  // table sits on the same page as that queue, so its verdict must stay hidden.
  // Anything the queue no longer offers cannot anchor a future grade: that
  // covers fully graded events and ones stranded because a remaining group is
  // unreviewable, whose evidence would otherwise be hidden forever. Use the
  // uncapped queue, or a research event sitting past the two-item cap would be
  // mistaken for finished.
  const awaitingGrade = new Set(reviewQueue(events, options).map(event => event.id));
  const rows = [...(events || [])].flatMap(event => {
    const groups = evidenceFrameReviewGroups(event);
    const viewRows = groups.flatMap(group => {
      const review = event.viewReviews?.[group.key];
      return review?.label && review.label !== "pending" ? [{ event, review, group }] : [];
    });
    if (viewRows.length) return viewRows;
    return event.review?.label && event.review.label !== "pending" ? [{ event, review: event.review, group: null }] : [];
  });
  return rows
    .sort((a, b) => new Date(b.review?.reviewedAt || 0) - new Date(a.review?.reviewedAt || 0))
    .map(({ event, review, group }) => {
      const rep = event.representative || {};
      const evidence = rep.evidence || {};
      const camera = group ? cameraForGroup(event, group) : event.evidence?.camera || {};
      const modelsWithheld = awaitingGrade.has(event.id);
      const v4Shadow = modelsWithheld ? null : (event.v4ShadowPredictions || []).at(-1) || null;
      const v5Shadow = modelsWithheld ? null : (event.v5ShadowPredictions || []).at(-1) || null;
      return {
        id: group ? `${event.id}::${group.key}` : event.id,
        candidateType: event.candidateType || "live_go",
        researchSource: event.researchSource || null,
        candidateClass: eventClass(event),
        archiveMethod: evidence.archiveMethod || null,
        grade: review.label,
        reviewedAt: review.reviewedAt || null,
        notes: review.notes || null,
        sceneSunlight: review.sceneSunlight || null,
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
          source: camera.source || event.evidence?.source || null,
          distanceKm: Number.isFinite(camera.distanceKm) ? camera.distanceKm : null,
          bearingDifference: Number.isFinite(camera.bearingDifference) ? camera.bearingDifference : null,
          viewQuality: camera.viewQuality || null,
          horizonSkyPct: Number.isFinite(camera.horizonSkyPct) ? camera.horizonSkyPct : null,
          nearestFrameOffsetMinutes: Number.isFinite(camera.nearestFrameOffsetMinutes) ? camera.nearestFrameOffsetMinutes : null,
          visibleBowFraction: Number.isFinite(camera.visibleBowFraction) ? camera.visibleBowFraction : null,
          bowArcOverlapDeg: Number.isFinite(camera.bowArcOverlapDeg) ? camera.bowArcOverlapDeg : null,
        },
        reviewStrength: reviewEvidenceStrength(camera.distanceKm, camera.bearingDifference, camera.viewQuality, camera.nearestFrameOffsetMinutes, camera.visibleBowFraction),
        v4Shadow, v5Shadow,
        sunlightAssessment: awaitingGrade.has(event.id)
          ? null
          : (event.researchAssessments || []).at(-1) || null,
        /* An assessment withheld by blinding is not the same as one that was
           never made, and the review table showed both as "Not assessed" --
           which reads as a broken pipeline when coverage is actually ~98%.
           This flag reports only that an assessment exists, never its verdict,
           so it cannot anchor the reviewer. */
        sunlightAssessmentWithheld: awaitingGrade.has(event.id)
          && (event.researchAssessments || []).length > 0,
      };
    });
}

export default async function handler(req, res) {
  if (req.method === "GET") {
    if (!authorized(req)) { json(res, 401, { ok: false, error: "Unauthorized." }); return; }
    const requestedLimit = Number(req.query?.limit) || 100;
    const label = String(req.query?.label || "").trim();
    const queue = String(req.query?.queue || "") === "1";
    const results = String(req.query?.results || "") === "1";
    const v4Daily = String(req.query?.v4Daily || "") === "1";
    const limit = Math.max(1, Math.min(requestedLimit, v4Daily ? 5000 : 1000));
    const source = String(req.query?.source || "").trim();
    const since = Date.parse(String(req.query?.since || ""));
    const until = Date.parse(String(req.query?.until || ""));
    const exclusionRegistry = queue || results ? await loadCameraExclusionRegistry() : null;
    const activeExclusions = exclusionRegistry ? activeCameraExclusionMap(exclusionRegistry) : new Map();
    const reviewOptions = { excludedCameraKeys: new Set(activeExclusions.keys()) };
    const loaded = v4Daily
      ? await loadGoEventsBySource("v4_shadow", limit)
      : results ? await loadReviewedGoEvents(limit)
      : Number.isFinite(since) && Number.isFinite(until)
      ? await loadGoEventsBetween(since, until, limit)
      : await loadGoEvents(limit);
    if (v4Daily) {
      const days = v4ShadowDaily(loaded);
      json(res, 200, { ok: true, days: days.length, events: days.reduce((sum, day) => sum + day.items.length, 0), items: days });
      return;
    }
    const scoped = source === "operational"
      ? loaded.filter(event => event.candidateType !== "research_possible")
      : source ? loaded.filter(event => event.researchSource === source) : loaded;
    const events = queue
      ? reviewQueue(scoped, reviewOptions)
      : scoped.filter(event => !label || event.review?.label === label
        || Object.values(event.viewReviews || {}).some(review => review?.label === label))
        .filter(event => !results || (event.review?.label && event.review.label !== "pending")
          || Object.values(event.viewReviews || {}).some(review => review?.label && review.label !== "pending"));
    const counts = {};
    for (const event of events) counts[event.review?.label || "pending"] = (counts[event.review?.label || "pending"] || 0) + 1;
    const items = queue ? reviewQueueItems(scoped, reviewOptions)
      : results ? reviewResults(events, reviewOptions)
        : events.map(event => reviewSafeEvent(event, reviewOptions));
    const withheld = queue || results
      ? { ...reviewWithholdingSummary(scoped, reviewOptions.excludedCameraKeys),
          activeCameraExclusions: activeExclusions.size }
      : null;
    json(res, 200, { ok: true, events: items.length, counts, withheld, items });
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
      event = body.cameraKey
        ? await labelGoEventView(body.eventId || body.id, body.cameraKey, body)
        : await labelGoEvent(body.eventId || body.id, body);
    } catch (error) {
      if (error?.statusCode === 400) { json(res, 400, { ok: false, error: error.message }); return; }
      throw error;
    }
    if (!event) { json(res, 404, { ok: false, error: "Event not found." }); return; }
    let cameraExclusion = null;
    if (body.cameraKey && body.label === 'no_camera') {
      const group = evidenceFrameReviewGroups(event).find(item => item.key === body.cameraKey);
      const first = group?.frames?.[0] || {};
      const stored = await appendCameraExclusion({
        cameraKey: body.cameraKey,
        source: first.source || event.evidence?.source || null,
        cameraName: first.cameraName || event.evidence?.camera?.name || null,
        eventId: event.id,
      });
      cameraExclusion = stored.entry;
    }
    json(res, 200, { ok: true, event: reviewSafeEvent(event), cameraExclusion });
    return;
  }
  res.setHeader("Allow", "GET, POST");
  json(res, 405, { ok: false, error: "Method not allowed." });
}
