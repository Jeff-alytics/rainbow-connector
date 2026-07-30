import test from "node:test";
import assert from "node:assert/strict";

import {
  applyCandidatePersistence,
  evaluateRequiredSources,
  sourceHealth,
} from "../api/artifact-policy.mjs";
import candidatesHandler from "../api/candidates.mjs";

const at = "2026-07-26T18:00:00Z";

test("source freshness blocks stale required feeds", () => {
  const now = new Date(at).getTime();
  assert.equal(sourceHealth("2026-07-26T17:55:00Z", 6, now).fresh, true);
  assert.equal(sourceHealth("2026-07-26T17:53:00Z", 6, now).fresh, false);
  const result = evaluateRequiredSources({
    radar: { observedAt: "2026-07-26T17:53:00Z" },
    goesCloud: { observedAt: "2026-07-26T17:50:00Z" },
    goesIrradiance: { observedAt: "2026-07-26T17:35:00Z" },
    sunlightModel: { observedAt: "2026-07-26T17:45:00Z" },
  }, now);
  assert.deepEqual(result.blocking, ["radar"]);
});

test("a candidate becomes GO after a matching consecutive scan", () => {
  const previous = {
    generatedAt: "2026-07-26T17:55:00Z",
    candidates: [{ lat: 35, lon: -86, persistence: { scanCount: 1 } }],
  };
  const current = {
    generatedAt: at,
    candidates: [{ lat: 35.05, lon: -86.03, verdict: "go", evidence: { score: 75 } }],
    possibleCandidates: [],
  };
  const result = applyCandidatePersistence(current, previous);
  assert.equal(result.candidates.length, 1);
  assert.equal(result.candidates[0].persistence.scanCount, 2);
});

test("a first-scan strict GO retains its GO verdict while awaiting persistence", () => {
  const current = {
    generatedAt: at,
    candidates: [{ lat: 35, lon: -86, verdict: "go", evidence: { score: 75 } }],
    possibleCandidates: [],
  };
  const result = applyCandidatePersistence(current, null);
  assert.equal(result.candidates.length, 0);
  assert.equal(result.possibleCandidates[0].persistencePending, true);
  assert.equal(result.possibleCandidates[0].verdict, "go");
  assert.equal(result.possibleCandidates[0].confidence, "new");
});

test("strong-geometry POSSIBLE is hidden until a matching second scan", () => {
  const fallback = {
    lat: 39.3,
    lon: -76.6,
    verdict: "watch",
    evidence: { selectionReason: "strong-radar-geometry-sunlight-uncertain" },
  };
  const first = applyCandidatePersistence({
    generatedAt: "2026-07-26T17:55:00Z",
    candidates: [],
    possibleCandidates: [fallback],
  }, null);
  assert.equal(first.possibleCandidates.length, 0);
  assert.equal(first.persistenceCandidates.length, 1);

  const second = applyCandidatePersistence({
    generatedAt: at,
    candidates: [],
    possibleCandidates: [{ ...fallback, lat: 39.31 }],
  }, first);
  assert.equal(second.possibleCandidates.length, 1);
  assert.equal(second.possibleCandidates[0].verdict, "watch");
  assert.equal(second.possibleCandidates[0].persistence.confirmed, true);
  assert.equal(second.persistenceCandidates.length, 0);
});

test("public candidate requests cannot trigger the live nationwide detector", async () => {
  const saved = {
    cron: process.env.SATELLITE_CRON_SECRET,
    publish: process.env.SATELLITE_PUBLISH_SECRET,
  };
  delete process.env.SATELLITE_CRON_SECRET;
  delete process.env.SATELLITE_PUBLISH_SECRET;
  const headers = {};
  let statusCode = null;
  let payload = null;
  const response = {
    setHeader(name, value) { headers[name] = value; },
    status(code) { statusCode = code; return this; },
    send(value) { payload = JSON.parse(value); return this; },
    end() { return this; },
  };
  try {
    await candidatesHandler({ method: "GET", query: { skipSatellite: "1" }, headers: {} }, response);
  } finally {
    if (saved.cron == null) delete process.env.SATELLITE_CRON_SECRET;
    else process.env.SATELLITE_CRON_SECRET = saved.cron;
    if (saved.publish == null) delete process.env.SATELLITE_PUBLISH_SECRET;
    else process.env.SATELLITE_PUBLISH_SECRET = saved.publish;
  }
  assert.equal(statusCode, 200);
  assert.equal(payload.stale, true);
  assert.equal(payload.dataStatus, "stale");
  assert.equal(headers["X-Rainbow-Candidate-Source"], "fallback");
});
