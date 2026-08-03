import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { ALERT_MIN_LINKED_SCANS, ALERT_RADIUS_KM, alertableGoCandidates, lookInstructions, nearestCandidate } from "../api/notify-alerts.mjs";
import { TURNSTILE_ACTION, verifyTurnstile } from "../api/turnstile.mjs";

test("email alerts require two accumulated linked scans", () => {
  const artifact = {
    candidates: [
      { id: "confirmed", verdict: "go", lat: 35, lon: -86, persistence: { confirmed: true, scanCount: 2 } },
      { id: "watch", verdict: "watch", lat: 37, lon: -88, persistence: { confirmed: true, scanCount: 3 } },
    ],
    possibleCandidates: [
      { id: "pending", verdict: "go", lat: 36, lon: -87, persistence: { confirmed: false, scanCount: 1 } },
      { id: "exceptional-first-scan", verdict: "go", lat: 36.5, lon: -87.5, persistence: { confirmed: false, scanCount: 1 } },
      { id: "possible", verdict: "watch", lat: 38, lon: -89 },
    ],
  };
  assert.equal(ALERT_MIN_LINKED_SCANS, 2);
  assert.deepEqual(alertableGoCandidates(artifact).map(item => item.id), ["confirmed"]);
});

test("email GO radius is fixed at the same 15 km used by ZIP cards", () => {
  assert.equal(ALERT_RADIUS_KM, 15);
  const sub = { lat: 0, lon: 0 };
  assert.ok(nearestCandidate(sub, [{ id: "inside", lat: 0, lon: 0.134 }]));
  assert.equal(nearestCandidate(sub, [{ id: "outside", lat: 0, lon: 0.136 }]), null);
});

test("GO emails translate model geometry into layperson look directions", () => {
  const look = lookInstructions({
    direction: { bearing: 106 },
    evidence: { sunElevationDeg: 12 },
  });
  assert.equal(look.compass, "east-southeast");
  assert.equal(look.direction, "Face east-southeast (about 106 degrees from north)");
  assert.equal(look.bowTop, 30);
  assert.equal(look.height, "Scan from the horizon up to about 30 degrees high");
  assert.equal(lookInstructions({ direction: {}, evidence: { rainbowArcDeg: null, sunElevationDeg: 18 } }).bowTop, 24);
});

test("Turnstile accepts only the alert action and allowed production hostname", async () => {
  const saved = { site: process.env.TURNSTILE_SITE_KEY, secret: process.env.TURNSTILE_SECRET_KEY, hosts: process.env.TURNSTILE_ALLOWED_HOSTNAMES };
  process.env.TURNSTILE_SITE_KEY = "site";
  process.env.TURNSTILE_SECRET_KEY = "secret";
  process.env.TURNSTILE_ALLOWED_HOSTNAMES = "therainbowconnector.com";
  const fakeFetch = async () => ({
    ok: true,
    json: async () => ({ success: true, action: TURNSTILE_ACTION, hostname: "therainbowconnector.com" }),
  });
  try {
    assert.equal((await verifyTurnstile("valid-token", "127.0.0.1", fakeFetch)).ok, true);
    const wrongHostFetch = async () => ({
      ok: true,
      json: async () => ({ success: true, action: TURNSTILE_ACTION, hostname: "attacker.example" }),
    });
    assert.equal((await verifyTurnstile("valid-token", "127.0.0.1", wrongHostFetch)).ok, false);
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      const name = key === "site" ? "TURNSTILE_SITE_KEY" : key === "secret" ? "TURNSTILE_SECRET_KEY" : "TURNSTILE_ALLOWED_HOSTNAMES";
      if (value == null) delete process.env[name]; else process.env[name] = value;
    }
  }
});

test("Turnstile stays hidden unless a visitor must interact", async () => {
  const page = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(page, /appearance: "interaction-only"/);
});
