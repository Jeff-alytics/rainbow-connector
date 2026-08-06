import assert from "node:assert/strict";
import test from "node:test";
import { matchManualRainbow, normalizeManualRainbow } from "../api/manual-rainbow-common.mjs";

test("manual rainbow input preserves uncertainty without entering blind review", () => {
  const item = normalizeManualRainbow({ sourceUrl: "https://x.com/example/status/1",
    observedAt: "2026-08-05T22:57:19Z", lat: 36.099, lon: -80.244,
    locationLabel: "Winston-Salem ballpark", matchRadiusKm: 25 }, new Date("2026-08-06T01:00:00Z"));
  assert.equal(item.evidenceLabel, "rainbow");
  assert.equal(item.locationCertainty, "approximate");
  assert.equal("review" in item, false);
});

test("manual rainbow matching keeps operational, V4, and V5 lanes distinct", () => {
  const manual = normalizeManualRainbow({ sourceUrl: "https://example.test/rainbow",
    observedAt: "2026-08-05T23:00:00Z", lat: 36, lon: -80, matchRadiusKm: 30 },
    new Date("2026-08-06T01:00:00Z"));
  const event = (id, researchSource, lat, lon) => ({ id, researchSource,
    candidateType: researchSource ? "research_possible" : "live_go", candidateClass: "GO",
    peakScore: 90, lastSeenAt: "2026-08-05T23:05:00Z", latestLocation: { lat, lon } });
  const result = matchManualRainbow(manual, [event("op", null, 36.05, -80),
    event("v4", "v4_shadow", 36.1, -80), event("v5", "v5_shadow", 40, -80)]);
  assert.equal(result.operational.matched, true);
  assert.equal(result.operational.modelDecision, "GO");
  assert.equal(result.v4.matched, true);
  assert.equal(result.v4.modelDecision, "POSSIBLE");
  assert.equal(result.v5.matched, false);
  assert.equal(result.v5.modelDecision, "POSSIBLE");
  assert.equal(result.exactReplayStatus, "pending");
});
