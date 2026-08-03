import test from "node:test";
import assert from "node:assert/strict";
import { matchNysmCameras, parseNysmPhotos, parseNysmStations } from "../api/nysm-common.mjs";

const stationPayload = { features: [{ geometry: { coordinates: [-73.8, 42.7] }, properties: { STID: "TEST", NAME: "Test Station", LAT: 42.7, LON: -73.8 } }] };
test("NYS Mesonet station and archive photos normalize to camera records", () => { const [station] = parseNysmStations(stationPayload); const [photo] = parseNysmPhotos(station, { photos: [{ timestamp: 1785628838, url: "https://api.nysmesonet.org/cam/index.php/image/nysm/TEST/1785628838", direction: 90 }] }); assert.equal(photo.stationId, "TEST"); assert.equal(photo.pan, 90); assert.equal(photo.imageUrl.includes("1785628838"), true); });
test("NYS Mesonet camera matching uses fixed camera direction", () => { const [station] = parseNysmStations(stationPayload); const [photo] = parseNysmPhotos(station, { photos: [{ timestamp: 1785628838, url: "https://example.com/test.jpg", direction: 90 }] }); const matches = matchNysmCameras({ representative: { lat: 42.7, lon: -73.8, direction: { bearing: 90 } } }, [photo]); assert.equal(matches.length, 1); });
