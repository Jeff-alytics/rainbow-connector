import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { aggregateV5CandidateScans, validV5CandidateScan } from "../api/v5-candidate-common.mjs";

function item(family, score, lane = "observed_core", overlap = false) {
  const row = {
    predictionId: `prediction-${family}-${score}`, familyEventId: family,
    detectedAt: "2026-08-05T12:00:00Z", lat: 40, lon: -90, score,
    rankWithinScan: 1, poolSize: 50, solarLane: lane,
    cohortMembership: ["expanded", ...(overlap ? ["overlap"] : [])],
    v4ExclusionReasons: overlap ? [] : ["v4_minimum_persistence"],
    acquisitionEnabled: false,
  };
  row.geometries = [{ geometryId: `${family}-${score}`, candidateId: row.predictionId,
    detectedAt: row.detectedAt, lat: row.lat, lon: row.lon, score, rankWithinScan: 1,
    solarLane: lane, sunElevationDeg: 12, bowBearingDeg: 90 }];
  row.geometrySelections = 1;
  return row;
}

function scan(at, items) {
  return { schemaVersion: "v5-candidate-scan.v1", scanTime: at,
    acquisitionEnabled: false, items };
}

test("V5 scan contract requires emission-off and accepts an uncapped item array", () => {
  const feed = scan("2026-08-05T12:00:00Z",
    Array.from({ length: 250 }, (_, index) => item(`family-${index}`, 100-index/10)));
  assert.equal(validV5CandidateScan(feed), true);
  assert.equal(validV5CandidateScan({ ...feed, acquisitionEnabled: true }), false);
});

test("daily feed retains every family while consolidating repeated scan geometries", () => {
  const days = aggregateV5CandidateScans([
    scan("2026-08-05T12:00:00Z", [item("a", 70), item("b", 60, "physical_audit")]),
    scan("2026-08-05T12:05:00Z", [{ ...item("a", 80), detectedAt: "2026-08-05T12:05:00Z" }]),
  ]);
  assert.equal(days.length, 1);
  assert.equal(days[0].items.length, 2);
  const family = days[0].items.find(row => row.familyEventId === "a");
  assert.equal(family.score, 80);
  assert.equal(family.scanSelections, 2);
  assert.equal(family.geometrySelections, 2);
  assert.equal(family.geometries.length, 2);
});

test("review page exposes the V5 board, elevation lanes, and post-grade comparison", async () => {
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /V5 candidates by day/);
  assert.match(page, /there is no top-two or per-day candidate cap/i);
  assert.match(page, /\/api\/v5-candidates\?days=7/);
  assert.match(page, /data-v5-filter="physical_audit"/);
  assert.match(page, /V4\.2 → V5/);
  assert.match(page, /item\.v5Shadow/);
});

test("review assessment endpoint accepts the compact V5 scan schema", async () => {
  const source = await readFile(new URL("../api/review-assessment.mjs", import.meta.url), "utf8");
  assert.match(source, /schemaVersion === "v5-candidate-scan\.v1"/);
  assert.match(source, /storeV5CandidateScan/);
  assert.match(source, /MAX_BODY_BYTES = 4 \* 1024 \* 1024/);
});
