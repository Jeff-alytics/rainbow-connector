import test from "node:test";
import assert from "node:assert/strict";

import { activeCameraExclusionMap, appendCameraExclusion, buildCameraExclusionEntry, loadCameraExclusionRegistry } from "../api/review-camera-exclusions.mjs";

test("bad-view exclusions are hash chained and expire for re-review", () => {
  const first = buildCameraExclusionEntry({
    cameraKey: "FAA%20WeatherCam:1:10", source: "FAA WeatherCam",
    cameraName: "Airport east", eventId: "event-1",
  }, undefined, "2026-08-01T00:00:00.000Z");
  const second = buildCameraExclusionEntry({
    cameraKey: "USGS%20NIMS:2:20", source: "USGS NIMS",
    cameraName: "River north", eventId: "event-2",
  }, first.recordHash, "2026-08-02T00:00:00.000Z");
  const registry = { schemaVersion: "review-camera-exclusions.v1", entries: [first, second] };
  assert.deepEqual([...activeCameraExclusionMap(registry, Date.parse("2026-08-03T00:00:00Z")).keys()],
    [first.cameraKey, second.cameraKey]);
  assert.deepEqual([...activeCameraExclusionMap(registry, Date.parse("2026-09-02T00:00:01Z")).keys()], []);
});

test("camera exclusion registry rejects content and chain mutation", () => {
  const first = buildCameraExclusionEntry({ cameraKey: "source:1:10", eventId: "event-1" },
    undefined, "2026-08-01T00:00:00.000Z");
  const registry = { schemaVersion: "review-camera-exclusions.v1", entries: [first] };
  assert.throws(() => activeCameraExclusionMap({
    ...registry, entries: [{ ...first, cameraName: "changed" }],
  }), /hash chain is invalid/);
  const second = buildCameraExclusionEntry({ cameraKey: "source:2:20", eventId: "event-2" },
    "f".repeat(64), "2026-08-02T00:00:00.000Z");
  assert.throws(() => activeCameraExclusionMap({ ...registry, entries: [first, second] }),
    /hash chain is invalid/);
});

test("camera exclusion storage appends once and is retry-idempotent", async () => {
  const originalFetch = global.fetch;
  const originalUrl = process.env.UPSTASH_REDIS_REST_URL;
  const originalToken = process.env.UPSTASH_REDIS_REST_TOKEN;
  process.env.UPSTASH_REDIS_REST_URL = "https://redis.test";
  process.env.UPSTASH_REDIS_REST_TOKEN = "token";
  let stored = null, writes = 0;
  global.fetch = async (_url, options) => {
    const command = JSON.parse(options.body);
    if (command[0] === "GET") return { ok: true, json: async () => ({ result: stored }) };
    if (command[0] === "EVAL") {
      stored = command[5]; writes++;
      return { ok: true, json: async () => ({ result: 1 }) };
    }
    throw new Error(`Unexpected command: ${command[0]}`);
  };
  try {
    const input = { cameraKey: "FAA:1:10", source: "FAA WeatherCam", eventId: "event-1" };
    const first = await appendCameraExclusion(input);
    const duplicate = await appendCameraExclusion(input);
    assert.equal(first.duplicate, false);
    assert.equal(duplicate.duplicate, true);
    assert.equal(writes, 1);
    assert.equal((await loadCameraExclusionRegistry()).entries.length, 1);
  } finally {
    global.fetch = originalFetch;
    if (originalUrl == null) delete process.env.UPSTASH_REDIS_REST_URL;
    else process.env.UPSTASH_REDIS_REST_URL = originalUrl;
    if (originalToken == null) delete process.env.UPSTASH_REDIS_REST_TOKEN;
    else process.env.UPSTASH_REDIS_REST_TOKEN = originalToken;
  }
});
