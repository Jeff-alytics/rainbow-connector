import test from "node:test";
import assert from "node:assert/strict";
import { buildImagesUrl, selectProbeSites, summarizeImages, FAA_ARCHIVE_PROBE_VERSION, MODEL_VERSIONS } from "./probe-faa-archive.mjs";

const catalog = [
  { id: 2, name: "Beta", state: "PA", lat: 40, lon: -75 },
  { id: 1, name: "Alpha", state: "OH", lat: 39, lon: -82 },
  { id: 3, name: "Gamma", state: "OH", lat: 38, lon: -83 },
];

test("buildImagesUrl pins an explicit UTC window", () => {
  const url = new URL(buildImagesUrl(42, "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"));
  assert.equal(url.pathname, "/api/sites/42/images");
  assert.equal(url.searchParams.get("startTime"), "2026-07-01T00:00:00.000Z");
  assert.equal(url.searchParams.get("endTime"), "2026-07-02T00:00:00.000Z");
});

test("site selection is deterministic and supports state, id, and sample filters", () => {
  assert.deepEqual(selectProbeSites(catalog).map(site => site.id), [1, 3, 2]);
  assert.deepEqual(selectProbeSites(catalog, { states: ["oh"], sampleSites: 1 }).map(site => site.id), [1]);
  assert.deepEqual(selectProbeSites(catalog, { siteIds: ["3", "1"] }).map(site => site.id), [1, 3]);
});

test("summary counts only in-window images and tracks usable frames by camera", () => {
  const summary = summarizeImages(catalog[0], { payload: [
    { cameraId: 9, imageDatetime: "2026-06-30T23:59:00Z", imageUri: "old" },
    { cameraId: 9, imageDatetime: "2026-07-01T01:00:00Z", imageUri: "one" },
    { cameraId: 9, imageDatetime: "2026-07-01T02:00:00Z" },
    { cameraId: 10, imageDatetime: "2026-07-01T03:00:00Z", imageUri: "two" },
  ] }, "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z");
  assert.equal(summary.imageCount, 3);
  assert.equal(summary.cameraCount, 2);
  assert.equal(summary.usableFrameCount, 2);
  assert.equal(summary.oldestImageAt, "2026-07-01T01:00:00.000Z");
  assert.equal(summary.newestImageAt, "2026-07-01T03:00:00.000Z");
});

test("manifest pins the study method versions", () => {
  assert.equal(FAA_ARCHIVE_PROBE_VERSION, "faa-retrospective-archive-probe-v1");
  assert.equal(MODEL_VERSIONS.sunlightV2, "sunlight-v2-shadow-2026-07-v3");
  assert.ok(MODEL_VERSIONS.sunlightV1);
});
