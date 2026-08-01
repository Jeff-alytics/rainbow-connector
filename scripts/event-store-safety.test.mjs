import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";

import { redisPipeline } from "../api/alert-common.mjs";
import { GO_EVENT_PREFIX, attachReviewAssessments, deleteHistoricalReviewEvent, evidenceFrameReviewKey,
  historicalSource, labelGoEvent, labelGoEventView,
  mutateGoEvent, newGoEvent, newResearchReviewEvent, saveHistoricalReviewEvent,
  syncConfirmedGallery } from "../api/go-event-common.mjs";

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

test("unmatched ledger identity does not reuse a nearby research event", async () => {
  const at = "2026-07-30T02:20:00Z";
  const existing = newResearchReviewEvent({ candidateId: "existing", disposition: "selected_research_possible",
    radarObservedAt: at, observer: { lat: 41.15, lon: -112.15 }, rain: {}, geometry: {},
    researchReview: { source: "opportunity_ledger", ledgerEventId: "ledger-Y" } });
  const assessment = { candidateId: "ledger-X-candidate", disposition: "selected_research_possible",
    radarObservedAt: at, observer: { lat: 41.01, lon: -112.01 }, rain: {}, geometry: {},
    researchReview: { source: "opportunity_ledger", ledgerEventId: "ledger-X" },
    idempotencyKey: "a".repeat(64) };
  const fake = fakeRedis({ [GO_EVENT_PREFIX + existing.id]: JSON.stringify(existing) });
  const result = await withRedis(fake, () => attachReviewAssessments([assessment]));
  assert.equal(result.created, 1);
  assert.equal(result.attached, 1);
  const storedExisting = JSON.parse(fake.values.get(GO_EVENT_PREFIX + existing.id));
  assert.equal(storedExisting.researchAssessments, undefined);
  const storedEvents = [...fake.values.entries()]
    .filter(([key]) => key.startsWith(GO_EVENT_PREFIX))
    .map(([, value]) => JSON.parse(value));
  const created = storedEvents.find(event => event.ledgerEventId === "ledger-X");
  assert.ok(created);
  assert.equal(created.researchAssessments.length, 1);
});
test("exact-point replay targets its named event only when time and location also match", async () => {
  const at = "2026-07-30T02:20:00Z";
  const target = newGoEvent({ detectedAt: at, lat: 41, lon: -112, score: 80, candidateClass: "GO", evidence: {} });
  const neighbor = newGoEvent({ detectedAt: at, lat: 41, lon: -112, score: 81, candidateClass: "GO", evidence: {} });
  neighbor.id += "-neighbor";
  const assessment = { targetEventId: target.id, candidateId: "replay", disposition: "selected_possible",
    decisionStage: "exact_point_causal_replay", radarObservedAt: at, observer: { lat: 41, lon: -112 },
    rain: {}, geometry: {}, idempotencyKey: "d".repeat(64) };
  const fake = fakeRedis({ [GO_EVENT_PREFIX + target.id]: JSON.stringify(target),
    [GO_EVENT_PREFIX + neighbor.id]: JSON.stringify(neighbor) });
  await withRedis(fake, () => attachReviewAssessments([assessment]));
  assert.equal(JSON.parse(fake.values.get(GO_EVENT_PREFIX + target.id)).researchAssessments[0].candidateId, "replay");
  assert.equal(JSON.parse(fake.values.get(GO_EVENT_PREFIX + neighbor.id)).researchAssessments, undefined);
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

function recordingRedis(seed = {}) {
  const values = new Map(Object.entries(seed));
  const commands = [];
  const fetch = async (_url, options) => {
    const body = JSON.parse(options.body);
    const batch = Array.isArray(body[0]) ? body : [body];
    batch.forEach(command => commands.push(command));
    const eventIds = () => [...values.keys()]
      .filter(key => key.startsWith(GO_EVENT_PREFIX))
      .map(key => key.slice(GO_EVENT_PREFIX.length));
    const reply = command => {
      const [name, ...args] = command;
      if (name === "GET") return { result: values.get(args[0]) ?? null };
      if (name === "MGET") return { result: args.map(key => values.get(key) ?? null) };
      if (name === "ZREVRANGEBYSCORE" || name === "ZREVRANGE") return { result: eventIds() };
      if (name === "EVAL") {
        const [, , eventKey, , next] = args;
        values.set(eventKey, next);
        return { result: 1 };
      }
      if (name === "SET") {
        if (args.includes("NX") && values.has(args[0])) return { result: null };
        values.set(args[0], args[1]);
        return { result: "OK" };
      }
      return { result: 1 };
    };
    if (Array.isArray(body[0])) return { ok: true, status: 200, json: async () => batch.map(reply) };
    return { ok: true, status: 200, json: async () => reply(body) };
  };
  return { values, commands, fetch, find: name => commands.find(command => command[0] === name) };
}

test("historical review events are claimed with NX so a concurrent seed cannot be clobbered", async () => {
  const candidate = { cameraId: "cam-1", observedAt: "2026-07-29T22:10:00Z", lat: 39.29, lon: -76.61,
    score: 70, bowBearing: 100, sourceKey: "cam-1:2026-07-29T22:10:00.000Z" };
  const fake = recordingRedis();
  const first = await withRedis(fake, () => saveHistoricalReviewEvent(candidate));
  assert.equal(first.stored, true);
  const write = fake.find("SET");
  assert.equal(write.includes("NX"), true, "historical write must be conditional");
  assert.equal(write.includes("EX"), true, "historical write must carry a TTL");
  // A second writer racing the same source key must not overwrite the first.
  const rival = { ...JSON.parse(fake.values.get(GO_EVENT_PREFIX + first.event.id)),
    review: { label: "rainbow", reviewedAt: "2026-07-29T23:00:00Z" } };
  fake.values.set(GO_EVENT_PREFIX + first.event.id, JSON.stringify(rival));
  const second = await withRedis(fake, () => saveHistoricalReviewEvent(candidate));
  assert.equal(second.stored, false);
  assert.equal(JSON.parse(fake.values.get(GO_EVENT_PREFIX + first.event.id)).review.label, "rainbow");
});

test("confirmed gallery rows expire with the event they describe", async () => {
  const event = newGoEvent({ detectedAt: "2026-07-30T02:20:00Z", lat: 41, lon: -112,
    score: 90, candidateClass: "GO", evidence: {} });
  event.review = { label: "rainbow", reviewedAt: "2026-07-30T03:00:00Z",
    confirmedFrames: [{ url: "https://frames.test/a.jpg", source: "FAA WeatherCam", distanceKm: 12 }] };
  event.evidence = { source: "FAA WeatherCam", camera: { name: "Test" },
    frames: [{ url: "https://frames.test/a.jpg", timeOffsetMinutes: 5 }] };
  const fake = recordingRedis();
  await withRedis(fake, () => syncConfirmedGallery(event));
  const write = fake.commands.find(command => command[0] === "SET" && String(command[1]).startsWith("rainbow:gallery:item:"));
  assert.ok(write, "gallery row must be written");
  assert.equal(write.includes("EX"), true, "gallery row must carry a TTL");
  assert.equal(Number(write[write.indexOf("EX") + 1]) > 0, true);
});

test("assessments that match nothing cost no Redis round trip", async () => {
  // The shadow worker sends one assessment per selected candidate -- dozens per
  // scan -- while the store holds only strict GO plus a handful of possibles, so
  // most legitimately match nothing. Resolving the match first keeps those off
  // the wire entirely; checking idempotency first cost a GET apiece.
  const at = "2026-07-30T02:20:00Z";
  const stored = newGoEvent({ detectedAt: at, lat: 41, lon: -112, score: 90,
    candidateClass: "GO", evidence: {} });
  const assessment = (candidateId, lat, lon) => ({
    candidateId, disposition: "selected_possible", radarObservedAt: at,
    observer: { lat, lon }, rain: {}, geometry: {},
    idempotencyKey: candidateId.padEnd(64, "0"),
  });
  const fake = recordingRedis({ [GO_EVENT_PREFIX + stored.id]: JSON.stringify(stored) });
  const result = await withRedis(fake, () => attachReviewAssessments([
    assessment("aa", 41, -112),      // matches the stored event
    assessment("bb", 20, -80),       // far away, matches nothing
    assessment("cc", 21, -81),       // far away, matches nothing
    assessment("dd", 22, -82),       // far away, matches nothing
  ]));
  assert.equal(result.attached, 1);
  assert.equal(result.unmatched, 3);
  const idempotencyReads = fake.commands.filter(command =>
    command[0] === "GET" && String(command[1]).startsWith("rainbow:review:assessment:"));
  assert.equal(idempotencyReads.length, 1,
    "only the matching assessment may read its idempotency key");
});

test("a redelivered assessment is still counted as a duplicate", async () => {
  const at = "2026-07-30T02:20:00Z";
  const stored = newGoEvent({ detectedAt: at, lat: 41, lon: -112, score: 90,
    candidateClass: "GO", evidence: {} });
  const key = "e".repeat(64);
  const assessment = { candidateId: "repeat", disposition: "selected_possible",
    radarObservedAt: at, observer: { lat: 41, lon: -112 }, rain: {}, geometry: {},
    idempotencyKey: key };
  const fake = recordingRedis({ [GO_EVENT_PREFIX + stored.id]: JSON.stringify(stored) });
  const first = await withRedis(fake, () => attachReviewAssessments([assessment]));
  assert.equal(first.attached, 1);
  const second = await withRedis(fake, () => attachReviewAssessments([assessment]));
  assert.equal(second.attached, 0);
  assert.equal(second.duplicates, 1, "the claim must still short-circuit a redelivery");
});

test("Python review callback payload matches the JavaScript research-event contract", () => {
  const assessment = JSON.parse(readFileSync(new URL("./fixtures/review-callback-contract-v1.json", import.meta.url), "utf8"));
  const event = newResearchReviewEvent(assessment);
  assert.equal(event.candidateType, "research_possible");
  assert.equal(event.researchSource, "opportunity_ledger");
  assert.equal(event.ledgerEventId, "rain-system-1");
});

/* An archived case must carry the network it came from. Recording an FAA case
   under the WebCOOS prefix mislabels the evidence, and because the delete path
   matches on the prefix it would also strand the record permanently. */
const FAA_CASE = { source: "faa", cameraId: "12076", observedAt: "2026-08-01T00:33:00Z",
  lat: 39.91145, lon: -105.11482, score: 80, bowBearing: 99.3,
  sourceKey: "faa:12076:2026-08-01T00:33:00.000Z" };

test("an FAA archived case is stored under its own prefix and candidate type", async () => {
  const fake = recordingRedis();
  const saved = await withRedis(fake, () => saveHistoricalReviewEvent(FAA_CASE));
  assert.equal(saved.stored, true);
  assert.equal(saved.event.id.startsWith("archive-faa-"), true,
    `FAA case must not be stored under another network's prefix, got ${saved.event.id}`);
  assert.equal(saved.event.candidateType, "historical_faa");
});

test("an archived FAA case can be deleted again", async () => {
  const fake = recordingRedis();
  const saved = await withRedis(fake, () => saveHistoricalReviewEvent(FAA_CASE));
  const removed = await withRedis(fake, () => deleteHistoricalReviewEvent(saved.event.id));
  assert.equal(removed, true, "an archived case the store accepted must also be removable");
});

test("WebCOOS remains the default source, unchanged", async () => {
  const fake = recordingRedis();
  const saved = await withRedis(fake, () => saveHistoricalReviewEvent({
    cameraId: "cam-9", observedAt: "2026-07-29T22:10:00Z", lat: 39.29, lon: -76.61,
    score: 70, bowBearing: 100, sourceKey: "cam-9:2026-07-29T22:10:00.000Z" }));
  assert.equal(saved.event.id.startsWith("archive-webcoos-"), true);
  assert.equal(saved.event.candidateType, "historical_webcoos");
});

test("an unrecognised archive source is refused rather than silently defaulted", () => {
  assert.throws(() => historicalSource("nexrad"), /Unknown historical review source/);
});
