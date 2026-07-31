import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { sanitizeSubscribers } from "../api/review-subscribers.mjs";

test("subscriber review output includes only safe fields for active confirmations", () => {
  const items = sanitizeSubscribers([
    {
      email: "older@example.com", zip: "70124", place: "New Orleans, LA", active: true,
      confirmedAt: "2026-07-26T12:00:00Z", lastAlertAt: null,
      unsubscribeToken: "must-not-leak", lat: 30, lon: -90,
    },
    {
      email: "newer@example.com", zip: "42406", place: "Corydon, KY", active: true,
      confirmedAt: "2026-07-27T12:00:00Z", unsubscribeToken: "also-secret",
    },
    { email: "inactive@example.com", zip: "10001", active: false },
  ]);
  assert.deepEqual(items.map(item => item.email), ["newer@example.com", "older@example.com"]);
  assert.deepEqual(Object.keys(items[0]), ["email", "zip", "place", "confirmedAt", "lastAlertAt"]);
  assert.equal(JSON.stringify(items).includes("must-not-leak"), false);
  assert.equal(JSON.stringify(items).includes('"lat"'), false);
});

test("one malformed subscriber key cannot empty the subscriber table", async () => {
  // The row-level try/catch in the handler is only reachable if the read
  // pipeline tolerates per-command errors. Without that, one bad key throws
  // and the whole table 500s.
  const rows = [
    JSON.stringify({ email: "a@example.com", zip: "70124", active: true, confirmedAt: "2026-07-26T12:00:00Z" }),
    JSON.stringify({ email: "c@example.com", zip: "10001", active: true, confirmedAt: "2026-07-28T12:00:00Z" }),
  ];
  const oldFetch = global.fetch, oldUrl = process.env.UPSTASH_REDIS_REST_URL, oldToken = process.env.UPSTASH_REDIS_REST_TOKEN;
  process.env.UPSTASH_REDIS_REST_URL = "https://redis.test";
  process.env.UPSTASH_REDIS_REST_TOKEN = "test";
  global.fetch = async (_url, options) => {
    const body = JSON.parse(options.body);
    if (!Array.isArray(body[0])) return { ok: true, status: 200, json: async () => ({ result: ["k1", "k2", "k3"] }) };
    return { ok: true, status: 200,
      json: async () => [{ result: rows[0] }, { error: "WRONGTYPE Operation against a key holding the wrong kind of value" }, { result: rows[1] }] };
  };
  const oldSecret = process.env.GO_REVIEW_SECRET;
  process.env.GO_REVIEW_SECRET = "test-review-secret";
  const sent = {};
  const res = { setHeader() {}, statusCode: 200,
    status(code) { sent.code = code; return this; },
    send(payload) { sent.body = payload; } };
  try {
    const { makeReviewToken } = await import("../api/review-auth-common.mjs");
    const { default: handler } = await import("../api/review-subscribers.mjs");
    await handler({ method: "GET", headers: { cookie: `rainbow_review=${makeReviewToken()}` } }, res);
  } finally {
    global.fetch = oldFetch;
    if (oldSecret == null) delete process.env.GO_REVIEW_SECRET; else process.env.GO_REVIEW_SECRET = oldSecret;
    if (oldUrl == null) delete process.env.UPSTASH_REDIS_REST_URL; else process.env.UPSTASH_REDIS_REST_URL = oldUrl;
    if (oldToken == null) delete process.env.UPSTASH_REDIS_REST_TOKEN; else process.env.UPSTASH_REDIS_REST_TOKEN = oldToken;
  }
  const payload = JSON.parse(sent.body);
  assert.equal(payload.ok, true, "the read fanout must not throw on one bad key");
  assert.equal(payload.subscribers, 2);
  assert.deepEqual(payload.items.map(item => item.email), ["c@example.com", "a@example.com"]);
});

test("private review page includes the subscriber table", async () => {
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /Email subscribers/);
  assert.match(page, /\/api\/review-subscribers/);
  assert.match(page, /Private tokens and coordinates are never shown/);
});
