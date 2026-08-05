import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  matchWebcoosCamera, parseWebcoosAssets, rankWebcoosElements, selectPendingWebcoosEvents, wedgeDirection,
} from "../api/webcoos-common.mjs";
import { reviewEvidenceStrength } from "../api/go-events.mjs";

function asset(overrides = {}) {
  return {
    disposition: { slug: "up" },
    data: {
      common: { slug: "coast", label: "Coast Camera" },
      properties: {
        location: { coordinates: [-75, 35] },
        state_or_territory: "North Carolina",
        wedge: { coordinates: [[[-75, 35], [-74.99, 34.99], [-74.985, 35], [-74.99, 35.01], [-75, 35]]] },
      },
    },
    feeds: [{ products: [{
      data: { common: { slug: "one-minute-stills" } },
      services: [{
        data: { common: { slug: "coast-one-minute-stills-s3" } },
        elements: { last_starting: "2026-07-28T18:55:00Z" },
      }],
    }] }],
    ...overrides,
  };
}

test("one-scan research can be captured by WebCOOS immediately", () => {
  const research = { id: "research", candidateType: "research_possible", scanCount: 1, peakScore: 99 };
  const go = { id: "go", candidateType: "live_go", scanCount: 1, peakScore: 70 };
  assert.deepEqual(selectPendingWebcoosEvents([research, go], 1).map(event => event.id), ["research"]);
});

test("WebCOOS catalog keeps current one-minute archives and published viewsheds", () => {
  const [camera] = parseWebcoosAssets({ results: [asset()] }, Date.parse("2026-07-28T19:00:00Z"));
  assert.equal(camera.id, "coast");
  assert.equal(camera.serviceSlug, "coast-one-minute-stills-s3");
  assert.equal(camera.state, "North Carolina");
  assert.ok(camera.cameraBearing > 80 && camera.cameraBearing < 100);
  assert.ok(camera.halfFovDeg > 30);
  assert.equal(camera.viewQuality, "unreviewed");
});

test("WebCOOS viewshed direction measures whether the bow falls inside the image", () => {
  const direction = wedgeDirection({ coordinates: [[[0, 0], [0.01, -0.01], [0.015, 0], [0.01, 0.01], [0, 0]]] }, 0, 0);
  assert.ok(direction.bearing > 80 && direction.bearing < 100);
  const event = { representative: { lat: 35, lon: -75, direction: { bearing: 110 } } };
  const match = matchWebcoosCamera(event, [
    { id: "visible", lat: 35.02, lon: -75, cameraBearing: 90, halfFovDeg: 30 },
    { id: "wrong-way", lat: 35.01, lon: -75, cameraBearing: 250, halfFovDeg: 30 },
  ]);
  assert.equal(match.camera.id, "visible");
  assert.equal(match.directionError, 0);
});

test("WebCOOS elements are selected nearest the GO and returned chronologically", () => {
  const centerMs = Date.parse("2026-07-28T18:05:00Z");
  const payload = { results: [
    { data: { properties: { url: "https://example.test/late.jpg", size: 10 }, extents: { temporal: { min: "2026-07-28T18:07:00Z" } } } },
    { data: { properties: { url: "https://example.test/early.jpg", size: 10 }, extents: { temporal: { min: "2026-07-28T18:03:00Z" } } } },
    { data: { properties: { url: "bad", size: 10 }, extents: { temporal: { min: "2026-07-28T18:05:00Z" } } } },
  ] };
  assert.deepEqual(rankWebcoosElements(payload, centerMs).map(frame => frame.url), [
    "https://example.test/early.jpg", "https://example.test/late.jpg",
  ]);
});

test("unreviewed WebCOOS views cannot create strong negative evidence", () => {
  assert.equal(reviewEvidenceStrength(10, 0, "unreviewed", 1), "limited");
});

test("notification workflow captures WebCOOS before ALERTCalifornia", async () => {
  const source = await readFile(new URL("../api/notify-alerts.mjs", import.meta.url), "utf8");
  assert.ok(source.indexOf("capturePendingWebcoosEvidence") < source.indexOf("capturePendingAlertCaEvidence(2)"));
});
