import test from "node:test";
import assert from "node:assert/strict";
import {
  matchNearbyDotCameras, parseCaltransDistrict, parseIowaFeatures,
  parseOhioCameras, parseWsdotCameras,
} from "../api/dot-camera-common.mjs";
import {
  angleDifference, matchAlertCaCameras, nearbyAlertCaCameras, parseAlertCaFeatures,
} from "../api/alertca-common.mjs";

test("DOT cameras are ranked by distance and limited", () => {
  const event = { representative: { lat: 39, lon: -75.5 } };
  const cameras = [
    { id: "far", lat: 39.2, lon: -75.5 },
    { id: "near", lat: 39.01, lon: -75.5 },
    { id: "outside", lat: 40, lon: -75.5 },
  ];
  const matches = matchNearbyDotCameras(event, cameras, 35, 2);
  assert.deepEqual(matches.map(match => match.camera.id), ["near", "far"]);
});

test("DOT matching rejects events without coordinates", () => {
  assert.deepEqual(matchNearbyDotCameras({}, [{ id: "x", lat: 39, lon: -75.5 }]), []);
});

test("DOT matching does not spend review slots on duplicate catalog rows", () => {
  const event = { representative: { lat: 42, lon: -93 } };
  const cameras = [
    { id: "same", lat: 42, lon: -93 },
    { id: "same", lat: 42, lon: -93 },
    { id: "other", lat: 42.01, lon: -93 },
  ];
  assert.deepEqual(matchNearbyDotCameras(event, cameras, 35, 3).map(match => match.camera.id), ["same", "other"]);
});

test("Caltrans records expose a normalized still image and optional stream", () => {
  const cameras = parseCaltransDistrict({ data: [{ cctv: {
    index: "17", inService: "true",
    location: { locationName: "I-5 test", latitude: "38.1", longitude: "-121.5", direction: "North" },
    imageData: { streamingVideoURL: "https://example.test/live.m3u8", static: { currentImageURL: "https://example.test/current.jpg" } },
  } }] }, 3);
  assert.equal(cameras.length, 1);
  assert.deepEqual(cameras[0], {
    id: "ca-03-17", name: "I-5 test", imageUrl: "https://example.test/current.jpg",
    streamUrl: "https://example.test/live.m3u8", pageUrl: "https://quickmap.dot.ca.gov/",
    updatedAt: null, lat: 38.1, lon: -121.5, routeDirection: "North",
  });
});

test("Iowa ArcGIS features expose image-first capture with video fallback", () => {
  const cameras = parseIowaFeatures({ features: [{
    attributes: { device_id: 42, Desc_: "US 20 test", ImageURL: "https://example.test/current.jpeg", VideoURL: null, latitude: 42.1, longitude: -93.5 },
  }] });
  assert.equal(cameras.length, 1);
  assert.equal(cameras[0].id, "ia-42");
  assert.equal(cameras[0].imageUrl, "https://example.test/current.jpeg");
  assert.equal(cameras[0].streamUrl, null);
});

test("OHGO camera sites are flattened into reviewable image views", () => {
  const cameras = parseOhioCameras({
    lastUpdated: "2026-07-28T13:00:00Z",
    results: [{ id: "123", latitude: 40.1, longitude: -82.9, description: "I-70 test",
      cameraViews: [{ direction: "View", largeUrl: "https://example.test/ohio.jpg" }] }],
  });
  assert.equal(cameras.length, 1);
  assert.equal(cameras[0].id, "oh-123-1");
  assert.equal(cameras[0].imageUrl, "https://example.test/ohio.jpg");
});

test("WSDOT cameras retain active image locations without exposing access codes", () => {
  const cameras = parseWsdotCameras([{ CameraID: 77, Title: "I-5 test", IsActive: true,
    DisplayLatitude: 47.6, DisplayLongitude: -122.3, ImageURL: "https://example.test/washington.jpg" }]);
  assert.equal(cameras.length, 1);
  assert.equal(cameras[0].id, "wa-77");
  assert.equal(cameras[0].imageUrl, "https://example.test/washington.jpg");
  assert.equal(JSON.stringify(cameras).includes("AccessCode"), false);
});
test("ALERTCalifornia records expose live direction, time, and image metadata", () => {
  const [camera] = parseAlertCaFeatures({ features: [{
    attributes: {
      OBJECTID: 12, cameraName: "Mountain 1", siteId: "mountain", positionPan: 91.5,
      positionTilt: -1.2, viewZoomScale: 1, viewTime: "2026-07-28 11:31:18-07:00",
      imageURL: "https://example.test/latest.jpg", cameraURL: "https://example.test/camera",
    },
    geometry: { x: -121.5, y: 38.1 },
  }] });
  assert.equal(camera.id, "alertca-12");
  assert.equal(camera.pan, 91.5);
  assert.equal(camera.observedAt, "2026-07-28T18:31:18.000Z");
  assert.equal(camera.imageUrl, "https://example.test/latest.jpg");
});

test("ALERTCalifornia matching requires both proximity and a bow-facing live view", () => {
  const event = { representative: { lat: 38, lon: -121.5, direction: { bearing: 90 } } };
  const cameras = [
    { id: "aligned", lat: 38.05, lon: -121.5, pan: 100 },
    { id: "wrong-way", lat: 38.01, lon: -121.5, pan: 220 },
    { id: "far", lat: 39, lon: -121.5, pan: 90 },
  ];
  assert.equal(angleDifference(350, 10), 20);
  assert.deepEqual(matchAlertCaCameras(event, cameras).map(match => match.camera.id), ["aligned"]);
  assert.deepEqual(nearbyAlertCaCameras(event, cameras).map(match => match.camera.id), ["wrong-way", "aligned"]);
});