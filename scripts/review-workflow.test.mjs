import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  makeReviewToken,
  reviewCookie,
  reviewTokenFromRequest,
  validReviewToken,
} from "../api/review-auth-common.mjs";
import { matchChartCameras, matchFaaCamera, matchFaaCameras, selectFaaFrames, selectPendingFaaEvents, visibleBowArc } from "../api/go-evidence-common.mjs";
import { evidenceFrameReviewGroups, reviewableCandidates } from "../api/go-event-common.mjs";
import { allCameraViewsReviewed, apparentSolarElevationDeg, reviewEvidenceStrength, reviewQueue, reviewQueueItems, reviewResults, reviewSafeEvent } from "../api/go-events.mjs";

test("near-horizon review geometry uses apparent solar elevation", () => {
  assert.ok(apparentSolarElevationDeg(0) > 0.45);
  assert.ok(apparentSolarElevationDeg(3.16) > 3.3);
  assert.ok(apparentSolarElevationDeg(3.16) < 3.5);
});

test("review sessions reject tampering and expiration", () => {
  const previous = process.env.GO_REVIEW_SECRET;
  process.env.GO_REVIEW_SECRET = "test-only-signing-secret-with-enough-entropy";
  try {
    const now = Date.UTC(2026, 6, 26);
    const token = makeReviewToken(now);
    assert.equal(validReviewToken(token, now + 1_000), true);
    assert.equal(validReviewToken(token.slice(0, -1) + (token.endsWith("a") ? "b" : "a"), now + 1_000), false);
    assert.equal(validReviewToken(token, now + 8 * 24 * 60 * 60 * 1_000), false);
    const cookie = reviewCookie(token);
    assert.match(cookie, /HttpOnly/);
    assert.match(cookie, /Secure/);
    assert.match(cookie, /SameSite=Strict/);
    assert.equal(reviewTokenFromRequest({ headers: { cookie } }), token);
  } finally {
    if (previous == null) delete process.env.GO_REVIEW_SECRET;
    else process.env.GO_REVIEW_SECRET = previous;
  }
});

test("FAA matching requires a nearby camera facing the predicted bow", () => {
  const event = { representative: { lat: 37.75, lon: -87.70, direction: { bearing: 90 } } };
  const catalog = [
    { id: "near", name: "Near", lat: 37.80, lon: -87.70, cameras: [
      { id: "wrong-way", bearing: 190, direction: "South" },
      { id: "aligned", bearing: 100, direction: "East" },
    ] },
    { id: "far", name: "Far", lat: 38.50, lon: -87.70, cameras: [
      { id: "far-aligned", bearing: 90, direction: "East" },
    ] },
  ];
  const match = matchFaaCamera(event, catalog);
  assert.equal(match.site.id, "near");
  assert.equal(match.camera.id, "aligned");
  assert.ok(match.distanceKm < 35);
  assert.ok(match.bearingDifference <= 50);
});

test("FAA arc matching finds both Utah viewpoints instead of one central bearing", () => {
  const event = { representative: { lat: 41.193604, lon: -112.00825,
    direction: { bearing: 112.3 }, evidence: { sunElevationDeg: 3.3 } } };
  const catalog = [
    { id: 969, name: "Ogden", lat: 41.193604, lon: -112.00825,
      cameras: [{ id: 13590, bearing: 90, direction: "East" }, { id: 13592, bearing: 270, direction: "West" }] },
    { id: 1077, name: "Bear River Valley Hospital", lat: 41.724174, lon: -112.18263,
      cameras: [{ id: 14000, bearing: 160, direction: "South" }] },
  ];
  const arc = visibleBowArc(event);
  assert.ok(arc.widthDeg > 80 && arc.widthDeg < 85);
  const matches = matchFaaCameras(event, catalog, { maxDistanceKm: 80, limit: 3 });
  assert.deepEqual(new Set(matches.map(match => match.camera.id)), new Set([13590, 14000]));
  assert.ok(matches.every(match => match.bowArcOverlapDeg >= 3));
});

test("FAA arc matching preserves an opposite bow-leg view when available", () => {
  const event = { representative: { lat: 41.193604, lon: -112.00825,
    direction: { bearing: 112.3 }, evidence: { sunElevationDeg: 3.3 } } };
  const catalog = [{ id: 1, name: "Near east", lat: 41.2, lon: -112,
    cameras: [{ id: "east", bearing: 95, direction: "East" }] },
  { id: 2, name: "Far south", lat: 41.7, lon: -112.18,
    cameras: [{ id: "south", bearing: 160, direction: "South" }] },
  { id: 3, name: "Another east", lat: 41.25, lon: -112,
    cameras: [{ id: "east-two", bearing: 100, direction: "East" }] }];
  const matches = matchFaaCameras(event, catalog, { maxDistanceKm: 80, limit: 3 });
  assert.ok(matches.some(match => match.camera.id === "south"));
});

test("FAA capture reserves one of two slots for persistent research evidence", () => {
  const go = { id: "go", candidateType: "live_go" }, old = { id: "old", candidateType: "live_possible" };
  const research = { id: "research", candidateType: "research_possible" };
  assert.deepEqual(selectPendingFaaEvents([go, old, research], 2).map(event => event.id), ["go", "research"]);
});

test("FAA frame selection requires multiple post-event views and keeps a balanced window", () => {
  const center = Date.parse("2026-07-30T01:00:00Z");
  const frame = (minutes, cameraId = 7) => ({
    cameraId, imageUri: `https://example.test/${minutes}.jpg`,
    imageDatetime: new Date(center + minutes * 60_000).toISOString(),
  });
  assert.deepEqual(selectFaaFrames([frame(-15), frame(-5), frame(5)], 7, center), []);
  assert.deepEqual(
    selectFaaFrames([frame(-25), frame(-15), frame(-5), frame(5), frame(15), frame(25), frame(2, 8)], 7, center)
      .map(item => Math.round((new Date(item.imageDatetime).getTime() - center) / 60_000)),
    [-15, -5, 5, 15, 25],
  );
});

test("Maryland CHART matching returns the nearest reviewable cameras", () => {
  const event = { representative: { lat: 39.20, lon: -76.70 } };
  const catalog = [
    { id: "third", name: "Third", lat: 39.30, lon: -76.70 },
    { id: "nearest", name: "Nearest", lat: 39.21, lon: -76.70 },
    { id: "second", name: "Second", lat: 39.25, lon: -76.70 },
    { id: "too-far", name: "Too far", lat: 40.20, lon: -76.70 },
  ];
  const matches = matchChartCameras(event, catalog, 35, 2);
  assert.deepEqual(matches.map(match => match.camera.id), ["nearest", "second"]);
  assert.ok(matches.every(match => match.distanceKm <= 35));
});

test("review queue contains only ungraded GO evidence, strongest first", () => {
  const event = (id, peakScore, label = "pending", frames = [{}]) => ({
    id, peakScore, lastSeenAt: "2026-07-26T23:00:00Z",
    review: { label }, evidence: { frames },
  });
  assert.deepEqual(
    reviewQueue([
      event("weaker", 72),
      event("graded", 99, "rainbow"),
      event("no-images", 98, "pending", []),
      event("stronger", 94),
    ]).map(item => item.id),
    ["stronger", "weaker"],
  );
});

test("FAA reviews stay hidden until at least two post-event frames exist", () => {
  const event = offsets => ({
    id: "faa-window", review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", frames: offsets.map((timeOffsetMinutes, index) => ({
      url: String(index), cameraName: "Test Airport · West", timeOffsetMinutes,
    })) },
  });
  assert.deepEqual(reviewQueue([event([-15, -6, 4])]), []);
  assert.equal(reviewQueue([event([-15, -6, 4, 14])]).length, 1);
});

test("all reviews omit FAA cameras beyond 40 km while retaining nearby views", () => {
  const frame = (cameraId, distanceKm, timeOffsetMinutes) => ({
    url: `${cameraId}-${timeOffsetMinutes}`, source: "FAA WeatherCam",
    siteId: cameraId, cameraId, cameraName: `Airport ${cameraId}`,
    distanceKm, timeOffsetMinutes,
  });
  const event = {
    id: "go-distance", candidateClass: "GO", review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", frames: [
      frame("near", 38, 5), frame("near", 38, 15),
      frame("far", 62, 5), frame("far", 62, 15),
    ] },
  };
  const items = reviewQueueItems([event]);
  assert.equal(items.length, 1);
  assert.equal(items[0].camera.name, "Airport near");
});

test("a GO with only a camera beyond 40 km never reaches Review", () => {
  const event = {
    id: "far-go", candidateClass: "GO", review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", frames: [
      { url: "far-1", siteId: "far", cameraId: 1, cameraName: "Far Airport", distanceKm: 52, timeOffsetMinutes: 4 },
      { url: "far-2", siteId: "far", cameraId: 1, cameraName: "Far Airport", distanceKm: 52, timeOffsetMinutes: 14 },
    ] },
  };
  assert.deepEqual(reviewQueueItems([event]), []);
});

test("review queue provides a refracted bow-top search height", () => {
  const event = {
    id: "low-sun-bow", review: { label: "pending" },
    representative: { evidence: { sunElevationDeg: 3.16 } },
    evidence: { frames: [{ url: "frame" }] },
  };
  const [item] = reviewQueueItems([event]);
  assert.equal(item.sunElevationDeg, 3.16);
  assert.ok(item.apparentSunElevationDeg > 3.3 && item.apparentSunElevationDeg < 3.5);
  assert.ok(item.expectedBowTopDeg > 38.5 && item.expectedBowTopDeg < 38.7);
});

test("review page explains low-sun red bows and expected bow height", async () => {
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /Expected bow top:/);
  assert.match(page, /Height guide showing the predicted bow top/);
  assert.match(page, /camera tilt and lens shape/);
  assert.match(page, /\[10,20,30,40\]/);
  assert.match(page, /faint red or orange arc/);
});

test("review queue prioritizes camera evidence quality before model score", () => {
  const event = (id, peakScore, distanceKm, bearingDifference) => ({
    id, peakScore, review: { label: "pending" },
    evidence: { frames: [{}], camera: { distanceKm, bearingDifference } },
  });
  assert.deepEqual(reviewQueue([
    event("limited-high-score", 99, 34, 45),
    event("strong-lower-score", 82, 20, 5),
    event("usable", 94, 30, 20),
  ]).map(item => item.id), ["strong-lower-score", "usable", "limited-high-score"]);
});

test("review queue places GO camera evidence before strong POSSIBLE evidence", () => {
  const event = (id, candidateClass, peakScore) => ({
    id, candidateClass, peakScore, review: { label: "pending" },
    evidence: { frames: [{}], camera: { distanceKm: 15, bearingDifference: 5 } },
  });
  assert.deepEqual(reviewQueue([
    event("possible", "POSSIBLE", 99),
    event("go", "GO", 70),
  ]).map(item => item.id), ["go", "possible"]);
});

test("one-scan research POSSIBLE waits for persistence before review", () => {
  const research = scanCount => ({ id: `research-${scanCount}`, candidateType: "research_possible",
    candidateClass: "POSSIBLE", scanCount, peakScore: 80, review: { label: "pending" },
    evidence: { frames: [{}], camera: { distanceKm: 10, bearingDifference: 5 } } });
  assert.deepEqual(reviewQueue([research(1)]), []);
  assert.deepEqual(reviewQueue([research(2)]).map(item => item.id), ["research-2"]);
});

test("research POSSIBLEs occupy at most two post-expansion review slots", () => {
  const research = index => ({ id: `research-${index}`, candidateType: "research_possible",
    candidateClass: "POSSIBLE", scanCount: 2, peakScore: 90-index, review: { label: "pending" },
    evidence: { frames: [{}], camera: { distanceKm: 10, bearingDifference: 5 } } });
  assert.equal(reviewQueueItems([1,2,3,4].map(research)).length, 2);
});

test("multiple camera views cannot expand research candidates beyond two review items", () => {
  const research = index => ({ id: `research-multi-${index}`, candidateType: "research_possible",
    candidateClass: "POSSIBLE", scanCount: 2, peakScore: 90-index, review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", camera: { distanceKm: 10, bearingDifference: 5 }, frames: [
      { url: `a-${index}`, siteId: index, cameraId: 1, cameraName: `Airport ${index} A`, distanceKm: 10, timeOffsetMinutes: 5 },
      { url: `b-${index}`, siteId: index + 10, cameraId: 2, cameraName: `Airport ${index} B`, distanceKm: 12, timeOffsetMinutes: 10 },
    ] } });
  const items = reviewQueueItems([research(1), research(2)]);
  assert.equal(items.length, 2);
  assert.equal(new Set(items.map(item => item.eventId)).size, 2);
  assert.deepEqual(items.map(item => item.eventId), ["research-multi-1", "research-multi-2"]);
});

test("a lone research event still fills both review slots with separate camera views", () => {
  // Per-event fairness must not forfeit the second slot when there is no second
  // event to give it to. A camera-gated candidate is worth two views.
  const frames = ["10", "20", "30"].flatMap(cameraId => [
    { url: `a${cameraId}`, source: "FAA WeatherCam", siteId: 1, cameraId, timeOffsetMinutes: 4, distanceKm: 12 },
    { url: `b${cameraId}`, source: "FAA WeatherCam", siteId: 1, cameraId, timeOffsetMinutes: 9, distanceKm: 12 },
  ]);
  const event = { id: "research-solo", candidateType: "research_possible", candidateClass: "POSSIBLE",
    scanCount: 2, lastSeenAt: "2026-07-30T20:00:00Z", review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", frames }, viewReviews: {}, representative: { evidence: {} } };
  const items = reviewQueueItems([event]);
  assert.equal(items.length, 2);
  assert.deepEqual(items.map(item => item.eventId), ["research-solo", "research-solo"]);
  assert.equal(new Set(items.map(item => item.cameraKey)).size, 2);
});

test("active reviews stay blinded while completed result rows expose their stored sunlight assessment", () => {
  const event = { id: "siblings", candidateType: "research_possible", review: { label: "pending" },
    researchAssessments: [{ sunlightState: "sunlit_supported",
      researchReview: { selectionReason: "strong geometry", currentDetectorDisposition: "rejected" },
      rain: { antiSolarRainArcSpanDeg: 35 } }], evidence: { frames: [
      { url: "a", source: "FAA WeatherCam", siteId: 1, cameraId: 10 },
      { url: "b", source: "FAA WeatherCam", siteId: 2, cameraId: 20 },
    ] }, viewReviews: {} };
  const groups = evidenceFrameReviewGroups(event);
  const queueItem = reviewQueueItems([{ ...event, scanCount: 2 }])[0];
  assert.equal("sunlightAssessmentAvailable" in queueItem, false);
  assert.equal("researchAssessments" in queueItem, false);
  assert.equal(queueItem.researchContext, null);
  event.viewReviews[groups[0].key] = { label: "no_rainbow", reviewedAt: "2026-07-30T02:30:00Z" };
  assert.equal(allCameraViewsReviewed(event), false);
  assert.equal("researchAssessments" in reviewSafeEvent(event), false);
  event.viewReviews[groups[1].key] = { label: "rainbow", reviewedAt: "2026-07-30T02:31:00Z" };
  assert.equal(allCameraViewsReviewed(event), true);
  assert.equal(reviewSafeEvent(event).researchAssessments.length, 1);
});

test("a completed result row hides its verdict only while a sibling view is still queued", () => {
  // The results table renders on the same page as the queue, so a verdict shown
  // here can anchor a grade the reviewer has not given yet.
  const frame = (cameraId, timeOffsetMinutes) => ({
    url: `${cameraId}-${timeOffsetMinutes}`, source: "FAA WeatherCam",
    siteId: cameraId, cameraId, distanceKm: 12, timeOffsetMinutes,
  });
  const build = frames => ({
    id: "two-airports", candidateType: "live_go", candidateClass: "GO", scanCount: 2,
    lastSeenAt: "2026-07-31T12:00:00Z", review: { label: "pending" },
    researchAssessments: [{ sunlightState: "sunlit_supported" }],
    evidence: { source: "FAA WeatherCam", frames }, viewReviews: {},
    representative: { evidence: {} },
  });

  // Reviewable: two post-event frames per airport, so the queue offers both.
  const reviewable = build([frame(10, 5), frame(10, 9), frame(20, 5), frame(20, 9)]);
  const groups = evidenceFrameReviewGroups(reviewable);
  reviewable.viewReviews[groups[0].key] = { label: "no_rainbow", reviewedAt: "2026-07-31T12:05:00Z" };
  assert.equal(reviewQueueItems([reviewable]).length, 1, "the second airport must still be queued");
  assert.equal(reviewResults([reviewable])[0].sunlightAssessment, null,
    "verdict must stay hidden while a sibling view awaits grading");

  // Fully graded: nothing left to anchor.
  reviewable.viewReviews[groups[1].key] = { label: "rainbow", reviewedAt: "2026-07-31T12:06:00Z" };
  assert.equal(reviewQueueItems([reviewable]).length, 0);
  assert.equal(reviewResults([reviewable]).every(row => row.sunlightAssessment?.sunlightState === "sunlit_supported"), true);

  // Stranded: the FAA gate needs two post-event frames across the event, and
  // this has one, so the queue never offers it. It can never become "fully
  // graded", and withholding here would hide its evidence forever.
  const stranded = build([frame(10, 5), frame(20, -5)]);
  const strandedGroups = evidenceFrameReviewGroups(stranded);
  stranded.viewReviews[strandedGroups[0].key] = { label: "no_rainbow", reviewedAt: "2026-07-31T12:05:00Z" };
  assert.equal(reviewQueueItems([stranded]).length, 0, "unreviewable event must not be queued");
  assert.equal(allCameraViewsReviewed(stranded), false);
  assert.equal(reviewResults([stranded])[0].sunlightAssessment?.sunlightState, "sunlit_supported",
    "stranded historical evidence must still be exposed");
});

test("review API serializes both list and grade responses through the anchoring guard", async () => {
  const source = await readFile(new URL("../api/go-events.mjs", import.meta.url), "utf8");
  assert.match(source, /events\.map\(reviewSafeEvent\)/);
  assert.match(source, /event:\s*reviewSafeEvent\(event\)/);
});

test("review labels ledger candidates without exposing them as public POSSIBLEs", async () => {
  const html = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(html, /Research POSSIBLE — ledger/);
  assert.match(html, /currentDetectorDisposition/);
  assert.match(html, /anti-solar rain span/);
});

test("FAA airports become separate review items with independent pending state", () => {
  const event = {
    id: "multi-airport", peakScore: 90, review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", frames: [
      { url: "a1", source: "FAA WeatherCam", siteId: 1, cameraId: 10, cameraName: "Airport A · East", timeOffsetMinutes: 4 },
      { url: "a2", source: "FAA WeatherCam", siteId: 1, cameraId: 10, cameraName: "Airport A · East", timeOffsetMinutes: 14 },
      { url: "b1", source: "FAA WeatherCam", siteId: 2, cameraId: 20, cameraName: "Airport B · North", timeOffsetMinutes: 5 },
      { url: "b2", source: "FAA WeatherCam", siteId: 2, cameraId: 20, cameraName: "Airport B · North", timeOffsetMinutes: 15 },
    ] },
  };
  const both = reviewQueueItems([event]);
  assert.equal(both.length, 2);
  assert.deepEqual(both.map(item => item.frames.map(frame => frame.url)), [["a1", "a2"], ["b1", "b2"]]);
  assert.notEqual(both[0].cameraKey, both[1].cameraKey);
  event.viewReviews = { [both[0].cameraKey]: { label: "no_rainbow" } };
  const remaining = reviewQueueItems([event]);
  assert.equal(remaining.length, 1);
  assert.equal(remaining[0].camera.name, "Airport B · North");
});

test("legacy camera frames split by camera name without reopening graded events", () => {
  const event = {
    id: "legacy-multi-airport", review: { label: "pending" },
    evidence: { source: "FAA WeatherCam", frames: [
      { url: "a1", source: "FAA WeatherCam", cameraName: "Airport A · East", timeOffsetMinutes: 4 },
      { url: "a2", source: "FAA WeatherCam", cameraName: "Airport A · East", timeOffsetMinutes: 14 },
      { url: "b1", source: "FAA WeatherCam", cameraName: "Airport B · West", timeOffsetMinutes: 5 },
      { url: "b2", source: "FAA WeatherCam", cameraName: "Airport B · West", timeOffsetMinutes: 15 },
    ] },
  };
  const items = reviewQueueItems([event]);
  assert.equal(items.length, 2);
  assert.deepEqual(items.map(item => item.camera.name), ["Airport A · East", "Airport B · West"]);
  event.review = { label: "no_rainbow", reviewedAt: "2026-07-30T01:00:00Z" };
  assert.deepEqual(reviewQueueItems([event]), []);
});

test("review candidates include GO plus only the strongest POSSIBLEs", () => {
  const candidate = (id, score, selectionReason = null) => ({
    id, verdict: "watch", evidence: { score, selectionReason },
  });
  const items = reviewableCandidates({
    candidates: [{ id: "go", lat: 40, lon: -90, verdict: "go", evidence: { score: 70 }, persistence: { confirmed: true } }],
    possibleCandidates: [
      candidate("strong", 55),
      candidate("weak", 49),
      candidate("fallback", 45, "strong-radar-geometry-sunlight-uncertain"),
    ],
  });
  assert.deepEqual(items.map(item => item.id), ["go", "strong", "fallback"]);
  assert.deepEqual(reviewableCandidates({
    possibleCandidates: [candidate("weak", 49)],
  }, { includeAllPossibles: true }).map(item => item.id), ["weak"]);
});

test("review page quietly refreshes without replacing the active candidate", async () => {
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /REVIEW_REFRESH_MS = 2 \* 60 \* 1000/);
  assert.match(page, /loadQueue\(\{ preserveCurrent: true \}\)/);
  assert.match(page, /incoming\.some\(item => item\.id === current\.id\)/);
  assert.match(page, /queue = \[current, \.\.\.incoming\.filter/);
  assert.match(page, /That review expired or was completed elsewhere/);
  assert.match(page, /id="refresh"/);
  assert.match(page, /refreshReviewPage/);
  assert.match(page, /Updated \$\{refreshedAt\}/);
});

test("review results expose grades beside model and camera evidence", () => {
  const items = reviewResults([
    {
      id: "older", peakScore: 82, scanCount: 2,
      review: { label: "possible", reviewedAt: "2026-07-26T23:00:00Z", notes: "partly blocked" },
      representative: { detectedAt: "2026-07-26T22:30:00Z", persistence: { scanCount: 4 }, evidence: {
        sunElevationDeg: 12.5, rainbowArcDeg: 29.5, directNormalIrradianceWm2: 260,
        rainIntensity: 0.6, observerRainIntensity: 0,
      } },
      evidence: { camera: { name: "Test Camera", distanceKm: 18.2, bearingDifference: 4 } },
    },
    {
      id: "newer", peakScore: 94, scanCount: 3,
      review: { label: "rainbow", reviewedAt: "2026-07-26T23:05:00Z" },
      representative: { evidence: {} }, evidence: {},
    },
    { id: "pending", review: { label: "pending" } },
  ]);
  assert.deepEqual(items.map(item => item.id), ["newer", "older"]);
  assert.equal(items[1].grade, "possible");
  assert.equal(items[1].scanCount, 4);
  assert.equal(items[1].rainbowArcDeg, 29.5);
  assert.equal(items[1].directNormalIrradianceWm2, 260);
  assert.equal(items[1].camera.distanceKm, 18.2);
  assert.equal(items[1].candidateClass, "GO");
});

test("review results derive bow-top height when older records omit it", () => {
  const [item] = reviewResults([{
    id: "derived", peakScore: 88, scanCount: 1,
    review: { label: "rainbow", reviewedAt: "2026-07-26T23:05:00Z" },
    representative: { persistence: { scanCount: 2 }, evidence: { sunElevationDeg: 18.5 } },
    evidence: {},
  }]);
  assert.equal(item.scanCount, 2);
  assert.equal(item.rainbowArcDeg, 23.5);
});

test("review results keep sunlight and rainbow grades separate by airport", () => {
  const event = {
    id: "split-result", review: { label: "pending" }, representative: { evidence: {} },
    evidence: { source: "FAA WeatherCam", frames: [
      { url: "a1", source: "FAA WeatherCam", siteId: 1, cameraId: 10, cameraName: "Sunny Airport", timeOffsetMinutes: 4 },
      { url: "a2", source: "FAA WeatherCam", siteId: 1, cameraId: 10, cameraName: "Sunny Airport", timeOffsetMinutes: 14 },
      { url: "b1", source: "FAA WeatherCam", siteId: 2, cameraId: 20, cameraName: "Cloudy Airport", timeOffsetMinutes: 5 },
      { url: "b2", source: "FAA WeatherCam", siteId: 2, cameraId: 20, cameraName: "Cloudy Airport", timeOffsetMinutes: 15 },
    ] },
  };
  const units = reviewQueueItems([event]);
  event.viewReviews = {
    [units[0].cameraKey]: { label: "rainbow", sceneSunlight: "sunlit", reviewedAt: "2026-07-30T01:00:00Z" },
    [units[1].cameraKey]: { label: "no_rainbow", sceneSunlight: "not_sunlit", reviewedAt: "2026-07-30T01:01:00Z" },
  };
  const results = reviewResults([event]);
  assert.deepEqual(results.map(item => [item.camera.name, item.grade, item.sceneSunlight]), [
    ["Cloudy Airport", "no_rainbow", "not_sunlit"],
    ["Sunny Airport", "rainbow", "sunlit"],
  ]);
});

test("review strength incorporates both distance and direction error", () => {
  assert.equal(reviewEvidenceStrength(23.3, 1), "strong");
  assert.equal(reviewEvidenceStrength(34.3, 25), "usable");
  assert.equal(reviewEvidenceStrength(20, 45), "limited");
  assert.equal(reviewEvidenceStrength(20, 5, "limited", 2), "limited");
  assert.equal(reviewEvidenceStrength(20, 5, "usable", 12), "limited");
  assert.equal(reviewEvidenceStrength(null, null), "unknown");
});

test("review page includes the model-versus-human results table", async () => {
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /Human grades compared with the evidence/);
  assert.match(page, /not a percent probability/);
  assert.match(page, /\/api\/go-events\?results=1/);
  assert.match(page, /Keep this image if it shows a rainbow/);
  assert.match(page, /type="checkbox"/);
  assert.match(page, /confirmedFrameUrls/);
  assert.match(page, /cameraKey:current\.cameraKey/);
  assert.match(page, /frame\.viewQuality/);
  assert.match(page, /frame\.timeOffsetMinutes/);
  assert.match(page, /Frame timing/);
  assert.match(page, /GO \/ Possible/);
  assert.match(page, /Live POSSIBLE/);
});

test("historical WebCOOS candidates stay visibly separate from live GO reviews", async () => {
  const [item] = reviewResults([{
    id: "archive-webcoos-example", candidateType: "historical_webcoos", peakScore: 90,
    review: { label: "rainbow", reviewedAt: "2026-07-28T12:00:00Z" },
    representative: { evidence: { archiveMethod: "weather plus radar" } }, evidence: {},
  }]);
  assert.equal(item.candidateType, "historical_webcoos");
  assert.equal(item.archiveMethod, "weather plus radar");
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /WebCOOS archive/);
  assert.match(page, /Archive screen/);
});
