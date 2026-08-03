import test from "node:test";
import assert from "node:assert/strict";
import { matchAlertWestCameras, parseAlertWestCameras } from "../api/alertwest-common.mjs";

const payload = [{ name: "Axis-TestMountain", source: "TestMountain", site: { id: "99", latitude: "40.1", longitude: "-120.2", state: "NV" }, image: { time: "2026-08-02T18:00:00Z", url: "https://img.example/test.jpg" }, position: { pan: "180", tilt: "-2" }, view: { line: "40.1,-120.2 40.0,-120.2" } }];
test("ALERTWest records map to Rainbow camera metadata", () => { const [camera] = parseAlertWestCameras(payload); assert.equal(camera.id, "alertwest-TestMountain"); assert.equal(camera.state, "NV"); assert.equal(camera.pan, 180); });
test("ALERTWest matching uses proximity and live pan", () => { const [camera] = parseAlertWestCameras(payload); const matches = matchAlertWestCameras({ representative: { lat: 40.1, lon: -120.2, direction: { bearing: 180 } } }, [camera]); assert.equal(matches.length, 1); assert.equal(matches[0].bearingDifference, 0); });
