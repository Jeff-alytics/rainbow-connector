import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  makeReviewToken,
  reviewCookie,
  reviewTokenFromRequest,
  validReviewToken,
} from "../api/review-auth-common.mjs";
import { matchChartCameras, matchFaaCamera } from "../api/go-evidence-common.mjs";
import { reviewableCandidates } from "../api/go-event-common.mjs";
import { reviewEvidenceStrength, reviewQueue, reviewResults } from "../api/go-events.mjs";

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
  assert.match(page, /queue = \[current, \.\.\.incoming\.filter/);
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
