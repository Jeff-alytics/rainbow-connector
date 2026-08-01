import assert from "node:assert/strict";
import test from "node:test";
import { matchNimsCamera, parseNimsTimestamp, rankNimsFrames, selectPendingNimsEvents } from "../api/usgs-nims-common.mjs";

test("one-scan research cannot consume a NIMS capture slot", () => {
  const research = { id: "research", candidateType: "research_possible", scanCount: 1, peakScore: 99 };
  const go = { id: "go", candidateType: "live_go", scanCount: 1, peakScore: 70 };
  assert.deepEqual(selectPendingNimsEvents([research, go], 1).map(event => event.id), ["go"]);
});

test("NIMS timestamps are normalized for browser and event timing", () => {
  assert.equal(parseNimsTimestamp("2026-07-27T23-15-03Z"), "2026-07-27T23:15:03Z");
  assert.equal(parseNimsTimestamp("bad"), null);
});

test("NIMS frames are selected nearest the GO and returned chronologically", () => {
  const center = new Date("2026-07-27T23:30:00Z").getTime();
  const rows = rankNimsFrames([
    { filename: "late.jpg", timestamp: "2026-07-27T23-45-00Z" },
    { filename: "near.jpg", timestamp: "2026-07-27T23-29-00Z" },
    { filename: "early.jpg", timestamp: "2026-07-27T23-15-00Z" },
    { filename: "bad.jpg", timestamp: "bad" },
  ], center, 3);
  assert.deepEqual(rows.map(row => row.filename), ["early.jpg", "near.jpg", "late.jpg"]);
  assert.equal(rows[1].observedAt, "2026-07-27T23:29:00Z");
});

test("NIMS matching prefers a reviewed usable view over a slightly nearer limited view", () => {
  const event = { representative: { lat: 40, lon: -75 } };
  const match = matchNimsCamera(event, [
    { id: "limited", lat: 40.01, lon: -75, viewQuality: "limited" },
    { id: "usable", lat: 40.08, lon: -75, viewQuality: "usable" },
    { id: "far", lat: 41, lon: -75, viewQuality: "usable" },
  ]);
  assert.equal(match.camera.id, "usable");
  assert.ok(match.distanceKm <= 35);
});
test("NIMS matching excludes events outside reviewed camera coverage", () => {
  const event = { representative: { lat: 35, lon: -100 } };
  assert.equal(matchNimsCamera(event, [
    { id: "far", lat: 40, lon: -75, viewQuality: "usable" },
  ]), null);
});