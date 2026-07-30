import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import test from "node:test";

import { redisPipeline } from "../api/alert-common.mjs";
import { GO_EVENT_PREFIX, attachReviewAssessments, mutateGoEvent, newGoEvent, newResearchReviewEvent } from "../api/go-event-common.mjs";

function response(result) {
  return { ok: true, status: 200, json: async () => ({ result }) };
}

function fakeRedis(seed = {}) {
  const values = new Map(Object.entries(seed));
  const eventIds = [...values.keys()].filter(key => key.startsWith(GO_EVENT_PREFIX)).map(key => key.slice(GO_EVENT_PREFIX.length));
  let collideOnce = false;
  const fetch = async (_url, options) => {
    const command = JSON.parse(options.body);
    const [name, ...args] = command;
    if (name === "GET") return response(values.get(args[0]) ?? null);
    if (name === "MGET") return response(args.map(key => values.get(key) ?? null));
    if (name === "ZREVRANGEBYSCORE") return response([...eventIds].reverse());
    if (name === "ZADD") {
      if (!eventIds.includes(String(args[2]))) eventIds.push(String(args[2]));
      return response(1);
    }
    if (name === "SET") {
      const [key, value] = args;
      const nx = args.includes("NX");
      if (nx && values.has(key)) return response(null);
      values.set(key, value);
      return response("OK");
    }
    if (name === "EVAL") {
      const key = args[2], expected = args[3], next = args[4];
      const current = values.get(key);
      if (collideOnce) {
        collideOnce = false;
        const changed = { ...JSON.parse(current), concurrentField: "preserved" };
        values.set(key, JSON.stringify(changed));
        return response(0);
      }
      const actual = current ? createHash("sha1").update(current).digest("hex") : null;
      if (actual !== expected) return response(0);
      values.set(key, next);
      return response(1);
    }
    throw new Error(`Unhandled fake Redis command ${name}`);
  };
  return { values, fetch, forceCollision: () => { collideOnce = true; } };
}

async function withRedis(fake, action) {
  const oldFetch = global.fetch;
  const oldUrl = process.env.UPSTASH_REDIS_REST_URL;
  const oldToken = process.env.UPSTASH_REDIS_REST_TOKEN;
  global.fetch = fake.fetch;
  process.env.UPSTASH_REDIS_REST_URL = "https://redis.test";
  process.env.UPSTASH_REDIS_REST_TOKEN = "test";
  try { return await action(); }
  finally {
    global.fetch = oldFetch;
    if (oldUrl == null) delete process.env.UPSTASH_REDIS_REST_URL; else process.env.UPSTASH_REDIS_REST_URL = oldUrl;
    if (oldToken == null) delete process.env.UPSTASH_REDIS_REST_TOKEN; else process.env.UPSTASH_REDIS_REST_TOKEN = oldToken;
  }
}

test("byte CAS retries and preserves a concurrent writer's fields", async () => {
  const event = newGoEvent({ detectedAt: "2026-07-30T02:20:00Z", lat: 41, lon: -112,
    score: 80, candidateClass: "GO", evidence: {} });
  const fake = fakeRedis({ [GO_EVENT_PREFIX + event.id]: JSON.stringify(event) });
  fake.forceCollision();
  await withRedis(fake, () => mutateGoEvent(event.id, current => {
    current.evidence = { ...(current.evidence || {}), frames: [{ url: "frame" }] };
    return current;
  }));
  const stored = JSON.parse(fake.values.get(GO_EVENT_PREFIX + event.id));
  assert.equal(stored.concurrentField, "preserved");
  assert.equal(stored.evidence.frames[0].url, "frame");
});

test("ledger assessment cannot attach to a nearby operational GO", async () => {
  const at = "2026-07-30T02:20:00Z";
  const live = newGoEvent({ detectedAt: at, lat: 41, lon: -112, score: 90, candidateClass: "GO", evidence: {} });
  const researchAssessment = { candidateId: "ledger", disposition: "selected_research_possible", radarObservedAt: at,
    observer: { lat: 41.01, lon: -112.01 }, rain: {}, geometry: { sunElevationDeg: 3, antiSolarBearingDeg: 100 },
    researchReview: { source: "opportunity_ledger", ledgerEventId: "rain-system-1" },
    idempotencyKey: "a".repeat(64) };
  const research = newResearchReviewEvent(researchAssessment);
  const fake = fakeRedis({ [GO_EVENT_PREFIX + live.id]: JSON.stringify(live),
    [GO_EVENT_PREFIX + research.id]: JSON.stringify(research) });
  await withRedis(fake, () => attachReviewAssessments([researchAssessment]));
  const storedLive = JSON.parse(fake.values.get(GO_EVENT_PREFIX + live.id));
  const storedResearch = JSON.parse(fake.values.get(GO_EVENT_PREFIX + research.id));
  assert.equal(storedLive.researchAssessments, undefined);
  assert.equal(storedResearch.researchAssessments.length, 1);
  assert.equal(storedResearch.ledgerEventId, "rain-system-1");
});

test("Redis pipeline surfaces a per-command Upstash failure", async () => {
  const fake = { fetch: async () => ({ ok: true, status: 200,
    json: async () => [{ result: "OK" }, { error: "ZADD failed" }] }) };
  await withRedis(fake, async () => {
    await assert.rejects(redisPipeline([["SET", "a", "1"], ["ZADD", "index", 1, "a"]]), /ZADD failed/);
  });
});

test("Python review callback payload matches the JavaScript research-event contract", () => {
  const python = process.env.PYTHON || (process.platform === "win32" ? ".venv\\Scripts\\python.exe" : "python3");
  const source = String.raw`
import json, sys
sys.path[:0] = ["worker", "api"]
from review_callback import build_payload
from sunlight_v2 import METHOD_VERSION

envelope = {
    "detectorRuleVersion": "contract-test",
    "radar": {"observedAt": "2026-07-30T02:20:00Z", "rainFootprintId": "fp-1"},
    "records": [{
        "candidateId": "contract-candidate",
        "disposition": "selected_research_possible",
        "decisionStage": "research",
        "features": {
            "observer": {"lat": 41.0, "lon": -112.0},
            "rain": {},
            "geometry": {"sunElevationDeg": 3.0, "antiSolarBearingDeg": 100.0},
            "researchReview": {"source": "opportunity_ledger", "ledgerEventId": "rain-system-1"},
            "sunlightV2": {"methodVersion": METHOD_VERSION, "sunlightState": "unresolved"},
        },
    }],
}
print(json.dumps(build_payload(envelope), separators=(",", ":")))
`;
  const result = spawnSync(python, ["-c", source], { cwd: process.cwd(), encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  const assessment = JSON.parse(result.stdout).assessments[0];
  const event = newResearchReviewEvent(assessment);
  assert.equal(event.candidateType, "research_possible");
  assert.equal(event.researchSource, "opportunity_ledger");
  assert.equal(event.ledgerEventId, "rain-system-1");
});
