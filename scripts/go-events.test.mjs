import assert from "node:assert/strict";
import test from "node:test";
import { confirmedGalleryRecord, compactGoDetection, matchingAssessmentEvent, matchingEvent, mergeDetection, newGoEvent, newResearchReviewEvent, researchDetectionFromAssessment } from "../api/go-event-common.mjs";

function detection(at, lat = 43, lon = -80, score = 80) {
  return compactGoDetection({ id: `candidate-${at}`, rank: 1, lat, lon,
    evidence: { score, sunElevationDeg: 18, goes: { decision: "go" } },
    persistence: { confirmed: true, scanCount: 2 } }, at);
}

test("nearby consecutive GO detections match the same event", () => {
  const event = newGoEvent(detection("2026-07-26T20:00:00Z"));
  const next = detection("2026-07-26T20:05:00Z", 43.08, -80.02, 91);
  assert.equal(matchingEvent([event], next, { radiusKm: 40, gapMinutes: 30 }), event);
  mergeDetection(event, next);
  assert.equal(event.scanCount, 2);
  assert.equal(event.detectionCount, 2);
  assert.equal(event.peakScore, 91);
  assert.equal(event.representative.candidateId, next.candidateId);
});

test("distant or much later detections form separate events", () => {
  const event = newGoEvent(detection("2026-07-26T20:00:00Z"));
  assert.equal(matchingEvent([event], detection("2026-07-26T20:05:00Z", 45, -80), { radiusKm: 40, gapMinutes: 30 }), null);
  assert.equal(matchingEvent([event], detection("2026-07-26T21:00:00Z"), { radiusKm: 40, gapMinutes: 30 }), null);
});

test("multiple detections in one scan do not inflate scan count", () => {
  const event = newGoEvent(detection("2026-07-26T20:00:00Z"));
  mergeDetection(event, detection("2026-07-26T20:00:00Z", 43.01, -80.01));
  assert.equal(event.scanCount, 1);
  assert.equal(event.detectionCount, 2);
});

test("GO records retain review-critical satellite and geometry evidence", () => {
  const row = detection("2026-07-26T20:00:00Z");
  assert.equal(row.persistence.confirmed, true);
  assert.equal(row.evidence.sunElevationDeg, 18);
  assert.deepEqual(row.evidence.goes, { decision: "go", positiveSources: [], negativeSources: [] });
});

test("private sunlight assessments match public review events by scan and location", () => {
  const event = newGoEvent(detection("2026-07-29T22:20:00Z", 41.55, -72.65));
  const assessment = { radarObservedAt: "2026-07-29T22:18:00Z", observer: { lat: 41.551, lon: -72.651 } };
  assert.equal(matchingAssessmentEvent([event], assessment), event);
  assert.equal(matchingAssessmentEvent([event], { ...assessment, observer: { lat: 42.5, lon: -72.65 } }), null);
});

test("geometry-first assessments create private POSSIBLE review events", () => {
  const assessment = {
    candidateId: "utah-live", disposition: "selected_research_possible",
    radarObservedAt: "2026-07-30T02:22:00Z", observer: { lat: 41.1218, lon: -112.0838 },
    rain: { lat: 41.095, lon: -111.995, distanceKm: 8, rateMmHr: 1.2,
      observerRateMmHr: 0, antiSolarRainArcSpanDeg: 100 },
    geometry: { sunElevationDeg: 3.3, antiSolarBearingDeg: 112.3, radarScore: 66.8 },
    researchReview: { ruleVersion: "geometry-first-review-2026-07-v1", rankWithinScan: 1 },
  };
  const detection = researchDetectionFromAssessment(assessment);
  assert.equal(detection.candidateClass, "POSSIBLE");
  assert.equal(detection.evidence.rainbowArcDeg, 38.7);
  const event = newResearchReviewEvent(assessment);
  assert.equal(event.candidateType, "research_possible");
  assert.equal(event.candidateClass, "POSSIBLE");
  assert.equal(event.scanCount, 1);
});

test("opportunity-ledger assessments stay private and retain their distinct source", () => {
  const assessment = {
    candidateId: "ledger-example", disposition: "selected_research_possible",
    radarObservedAt: "2026-07-30T02:27:00Z", observer: { lat: 41.12, lon: -112.08 },
    rain: { lat: 41.10, lon: -112.00, distanceKm: 8, rateMmHr: 1,
      antiSolarRainArcSpanDeg: 42 },
    geometry: { sunElevationDeg: 3.1, antiSolarBearingDeg: 112.3, radarScore: null },
    researchReview: { source: "opportunity_ledger", ruleVersion: "opportunity-ledger-review-2026-07-v1",
      currentDetectorDisposition: "not_generated_as_detector_candidate", rankWithinScan: 1 },
  };
  const detection = researchDetectionFromAssessment(assessment);
  assert.equal(detection.evidence.selectionReason, "opportunity-ledger-camera-gated-review-only");
  assert.equal(detection.evidence.currentDetectorDisposition, "not_generated_as_detector_candidate");
  const event = newResearchReviewEvent(assessment);
  assert.equal(event.candidateType, "research_possible");
  assert.equal(event.researchSource, "opportunity_ledger");
  assert.equal(event.candidateClass, "POSSIBLE");
});

test("V4.2 assessments retain frozen sunlight-decided prediction metadata", () => {
  const assessment = {
    candidateId: "v4-example", disposition: "selected_research_possible",
    radarObservedAt: "2026-08-05T20:10:00Z", observer: { lat: 40.1, lon: -90.2 },
    rain: { lat: 40.1, lon: -90.0, distanceKm: 17, rateMmHr: 2, observerRateMmHr: 0 },
    geometry: { sunElevationDeg: 12, antiSolarBearingDeg: 91, radarScore: 98 },
    researchReview: { source: "v4_shadow", ruleVersion: "v4-shadow-review-2026-08-v2",
      modelVersion: "causal-bounded-persistence-radar-v2", lane: "go",
      classification: "GO_SUNLIT_SUPPORTED", rankWithinScan: 1,
      poolSize: 7, score: 92.5, hasMatchedCamera: false, ledgerEventId: "rain-family-1",
      v4Prediction: { predictionId: "v4-p1", predictionSha256: "a".repeat(64),
        mechanicalFreezeContentSha256: "b".repeat(64), scanTime: "2026-08-05T20:10:00Z",
        lane: "go", classification: "GO_SUNLIT_SUPPORTED", sunlightState: "sunlit_supported",
        rankWithinScan: 1, poolSize: 7 } },
  };
  const detection = researchDetectionFromAssessment(assessment);
  assert.equal(detection.label, "V4.2 shadow GO");
  assert.equal(detection.score, 92.5);
  const event = newResearchReviewEvent(assessment);
  assert.equal(event.researchSource, "v4_shadow");
  assert.equal(event.v4ShadowPredictions[0].predictionId, "v4-p1");
  assert.equal(event.v4ShadowPredictions[0].classification, "GO_SUNLIT_SUPPORTED");
  assert.equal(event.v4ShadowPredictions[0].hasMatchedCamera, false);
});

test("ledger identities do not fall back to the research storm corridor", () => {
  const first = newResearchReviewEvent({ candidateId: "one", disposition: "selected_research_possible",
    radarObservedAt: "2026-07-30T02:22:00Z", observer: { lat: 41.1218, lon: -112.0838 },
    rain: {}, geometry: { sunElevationDeg: 3.3, antiSolarBearingDeg: 112, radarScore: 67 }, researchReview: {} });
  const next = { radarObservedAt: "2026-07-30T02:26:00Z", observer: { lat: 41.2467, lon: -112.0609 }, researchReview: { ledgerEventId: "ledger-X" } };
  assert.equal(matchingAssessmentEvent([first], next, { radiusKm: 35, gapMinutes: 12 }), null);
});

test("operational detections cannot match or merge into research events", () => {
  const research = newResearchReviewEvent({ candidateId: "research", disposition: "selected_research_possible",
    radarObservedAt: "2026-07-30T02:22:00Z", observer: { lat: 41.12, lon: -112.08 }, rain: {},
    geometry: { sunElevationDeg: 3.3, antiSolarBearingDeg: 112, radarScore: 67 }, researchReview: {} });
  const operational = detection("2026-07-30T02:25:00Z", 41.12, -112.08);
  assert.equal(matchingEvent([research], operational, { radiusKm: 40, gapMinutes: 30 }), null);
  assert.throws(() => mergeDetection(research, operational), /cannot share an event/);
});

test("confirmed rainbow gallery records permanently retain frame links and evidence", () => {
  const event = {
    id: "go-example", firstSeenAt: "2026-07-26T23:00:00Z", peakScore: 91, scanCount: 1,
    review: {
      label: "rainbow",
      reviewedAt: "2026-07-27T01:00:00Z",
      confirmedFrames: [
        { url: "https://blob.example/1.jpg", observedAt: "2026-07-26T23:04:00Z", source: "Maryland CHART", cameraName: "MD 100 at Test Rd", distanceKm: 6.4 },
        { url: "https://blob.example/2.jpg", observedAt: "2026-07-26T23:09:00Z" },
      ],
    },
    representative: {
      detectedAt: "2026-07-26T23:05:00Z", persistence: { scanCount: 3 },
      evidence: { sunElevationDeg: 17, directNormalIrradianceWm2: 420 },
    },
    evidence: {
      camera: { name: "Example", distanceKm: 12, bearingDifference: 3 },
      frames: [
        { url: "https://blob.example/1.jpg", observedAt: "2026-07-26T23:04:00Z" },
        { url: "https://blob.example/2.jpg", observedAt: "2026-07-26T23:09:00Z" },
      ],
    },
  };
  const record = confirmedGalleryRecord(event);
  assert.equal(record.id, "go-example");
  assert.equal(record.scanCount, 3);
  assert.equal(record.frames.length, 2);
  assert.equal(record.frames[0].url, "https://blob.example/1.jpg");
  assert.equal(record.frames[0].source, "Maryland CHART");
  assert.equal(record.frames[0].cameraName, "MD 100 at Test Rd");
  assert.equal(record.frames[0].distanceKm, 6.4);
  assert.equal(record.frames[1].url, "https://blob.example/2.jpg");
  assert.equal(record.evidence.directNormalIrradianceWm2, 420);
  assert.equal(confirmedGalleryRecord({ ...event, review: { label: "rainbow" } }), null);
  assert.equal(confirmedGalleryRecord({ ...event, review: { label: "possible" } }), null);
});

test("confirmed gallery accepts rainbow frames graded on one camera view", () => {
  const event = {
    id: "split-gallery", review: { label: "pending" }, representative: { evidence: {} },
    viewReviews: {
      "FAA:1:10": {
        label: "rainbow", reviewedAt: "2026-07-30T01:00:00Z",
        confirmedFrames: [{ url: "https://blob.example/airport.jpg", cameraName: "Airport East" }],
      },
      "FAA:2:20": { label: "no_rainbow", reviewedAt: "2026-07-30T01:01:00Z", confirmedFrames: [] },
    },
  };
  const record = confirmedGalleryRecord(event);
  assert.equal(record.frames.length, 1);
  assert.equal(record.frames[0].cameraName, "Airport East");
});
