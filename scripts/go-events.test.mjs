import assert from "node:assert/strict";
import test from "node:test";
import { confirmedGalleryRecord, compactGoDetection, matchingAssessmentEvent, matchingEvent, mergeDetection, newGoEvent } from "../api/go-event-common.mjs";

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
