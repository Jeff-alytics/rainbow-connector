import assert from "node:assert/strict";
import test from "node:test";
import { negativeEvidence } from "./negative-fixture-common.mjs";

function event({ bearing = 5, distance = 10, source = "FAA WeatherCam", quality = null,
  visibleBowFraction = null, offsets = [0, 3, 6, 9, 12] } = {}) {
  const target = Date.parse("2026-07-29T20:00:00Z");
  return {
    representative: { detectedAt: new Date(target).toISOString() },
    evidence: {
      source,
      camera: { bearingDifference: bearing, distanceKm: distance, viewQuality: quality, visibleBowFraction },
      frames: offsets.map(minutes => ({ observedAt: new Date(target + minutes * 60000).toISOString() })),
    },
  };
}

test("multi-frame timely known-bearing FAA review can be a calibration negative", () => {
  const result = negativeEvidence(event());
  assert.equal(result.calibrationEligible, true);
  assert.ok(result.weight >= 0.5);
});

test("unknown camera bearing forces negative evidence weight to zero", () => {
  const result = negativeEvidence(event({ bearing: null }));
  assert.equal(result.weight, 0);
  assert.equal(result.calibrationEligible, false);
  assert.ok(result.eligibilityReasons.includes("camera_bearing_unknown"));
});

test("a camera covering less than half the visible bow has zero negative weight", () => {
  const result = negativeEvidence(event({ visibleBowFraction: 0.42 }));
  assert.equal(result.weight, 0);
  assert.ok(result.eligibilityReasons.includes("less_than_half_of_visible_bow_in_view"));
});

test("single-frame and unreviewed evidence stays visible but cannot calibrate", () => {
  const result = negativeEvidence(event({ source: "WebCOOS / contributing camera partner", quality: "unreviewed", offsets: [1] }));
  assert.equal(result.calibrationEligible, false);
  assert.ok(result.weight > 0 && result.weight < 0.5);
  assert.ok(result.eligibilityReasons.includes("single_or_missing_frame"));
  assert.ok(result.eligibilityReasons.includes("view_quality_not_calibration_grade"));
});
