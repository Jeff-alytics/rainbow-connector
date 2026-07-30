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

test("private review page includes the subscriber table", async () => {
  const page = await readFile(new URL("../review.html", import.meta.url), "utf8");
  assert.match(page, /Email subscribers/);
  assert.match(page, /\/api\/review-subscribers/);
  assert.match(page, /Private tokens and coordinates are never shown/);
});
