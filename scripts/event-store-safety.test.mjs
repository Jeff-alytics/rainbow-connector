import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";

import { redisPipeline } from "../api/alert-common.mjs";
import { GO_EVENT_PREFIX, attachReviewAssessments, evidenceFrameReviewKey, labelGoEvent, labelGoEventView,
  mutateGoEvent, newGoEvent, newResearchReviewEvent } from "../api/go-event-common.mjs";

function response(result) {
  return { ok: true, status: 200, json: async () => ({ result }) };
}

function fakeRedis(seed = {}) {
  const values = new Map(Object.entries(seed));
  const eventIds = [...values.keys()].filter(key => key.startsWith(GO_EVENT_PREFIX)).map(key => key.slice(GO_EVENT_PREFIX.length));
  let collideOnce = false;
  let evalErrorOnce = false;
  const fetch = async (_url, options) => {
    const command = JSON.parse(options.body);
    if (Array.isArray(command[0])) {
      return { ok: true, status: 200, json: async () => command.map(() => ({ result: 1 })) };
    }
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
      if (evalErrorOnce) { evalErrorOnce = false; throw new Error("injected event write failure"); }
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
  return { values, fetch, forceCollision: () => { collideOnce = true; },
    forceEvalError: () => { evalErrorOnce = true; } };
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
  const existingAssessment = { ...researchAssessment, observer: { lat: 41.15, lon: -112.15 } };
  const research = newResearchReviewEvent(existingAssessment);
  const fake = fakeRedis({ [GO_EVENT_PREFIX + live.id]: JSON.stringify(live),
    [GO_EVENT_PREFIX + research.id]: JSON.stringify(research) });
  await withRedis(fake, () => attachReviewAssessments([researchAssessment]));
  const storedLive = JSON.parse(fake.values.get(GO_EVENT_PREFIX + live.id));
  const storedResearch = JSON.parse(fake.values.get(GO_EVENT_PREFIX + research.id));
  assert.equal(storedLive.researchAssessments, undefined);
  assert.equal(storedResearch.researchAssessments.length, 1);
  assert.equal(storedResearch.ledgerEventId, "rain-system-1");
});

test("human event and view labels retry CAS without losing concurrent fields", async () => {
  const detection = { detectedAt: "2026-07-30T02:20:00Z", lat: 41, lon: -112,
    score: 80, candidateClass: "GO", evidence: {} };
  for (const view of [false, true]) {
    const event = newGoEvent(detection);
    event.id += view ? "-view" : "-event";
    event.evidence = { source: "FAA WeatherCam", frames: [{ url: "frame", source: "FAA WeatherCam", siteId: 1, cameraId: 2 }] };
    const fake = fakeRedis({ [GO_EVENT_PREFIX + event.id]: JSON.stringify(event) });
    fake.forceCollision();
    await withRedis(fake, () => view
      ? labelGoEventView(event.id, evidenceFrameReviewKey(event.evidence.frames[0]), { label: "no_rainbow" })
      : labelGoEvent(event.id, { label: "no_rainbow" }));
    const stored = JSON.parse(fake.values.get(GO_EVENT_PREFIX + event.id));
    assert.equal(stored.concurrentField, "preserved");
    assert.equal(view ? Object.values(stored.viewReviews)[0].label : stored.review.label, "no_rainbow");
  }
});

test("one malformed assessment cannot prevent a later record from attaching", async () => {
  const at = "2026-07-30T02:20:00Z";
  const first = newGoEvent({ detectedAt: at, lat: 35, lon: -100, score: 80, candidateClass: "GO", evidence: {} });
  const second = newGoEvent({ detectedAt: at, lat: 45, lon: -90, score: 81, candidateClass: "GO", evidence: {} });
  const assessment = (candidateId, lat, lon, key) => ({ candidateId, disposition: "selected_possible",
    radarObservedAt: at, observer: { lat, lon }, rain: {}, geometry: {}, idempotencyKey: key.repeat(64) });
  const fake = fakeRedis({ [GO_EVENT_PREFIX + first.id]: JSON.stringify(first),
    [GO_EVENT_PREFIX + second.id]: JSON.stringify(second) });
  fake.forceEvalError();
  const result = await withRedis(fake, () => attachReviewAssessments([
    assessment("broken", 35, -100, "b"), assessment("healthy", 45, -90, "c"),
  ]));
  assert.equal(result.errors.length, 1);
  assert.equal(result.attached, 1);
  assert.equal(JSON.parse(fake.values.get(GO_EVENT_PREFIX + second.id)).researchAssessments[0].candidateId, "healthy");
});

test("Redis pipeline surfaces a per-command Upstash failure", async () => {
  const fake = { fetch: async () => ({ ok: true, status: 200,
    json: async () => [{ result: "OK" }, { error: "ZADD failed" }] }) };
  await withRedis(fake, async () => {
    await assert.rejects(redisPipeline([["SET", "a", "1"], ["ZADD", "index", 1, "a"]]), /ZADD failed/);
  });
});

test("Redis pipeline can preserve per-key tolerance for read fanout", async () => {
  const fake = { fetch: async () => ({ ok: true, status: 200,
    json: async () => [{ result: "value" }, { error: "one key failed" }] }) };
  await withRedis(fake, async () => {
    const rows = await redisPipeline([["GET", "a"], ["GET", "b"]], { allowCommandErrors: true });
    assert.equal(rows[0].result, "value");
    assert.equal(rows[1].error, "one key failed");
  });
});

test("Python review callback payload matches the JavaScript research-event contract", () => {
  const assessment = JSON.parse(readFileSync(new URL("./fixtures/review-callback-contract-v1.json", import.meta.url), "utf8"));
  const event = newResearchReviewEvent(assessment);
  assert.equal(event.candidateType, "research_possible");
  assert.equal(event.researchSource, "opportunity_ledger");
  assert.equal(event.ledgerEventId, "rain-system-1");
});
