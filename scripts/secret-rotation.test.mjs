import test from "node:test";
import assert from "node:assert/strict";

import { verifySecret } from "../api/alert-common.mjs";
import { verifyPublishSecret } from "../api/satellite-candidates.mjs";

function request(secret) {
  return { headers: { authorization: `Bearer ${secret}` }, query: {} };
}

test("notification endpoint accepts old and next secrets during rotation", () => {
  const saved = [process.env.ALERT_NOTIFY_SECRET, process.env.ALERT_NOTIFY_SECRET_NEXT];
  process.env.ALERT_NOTIFY_SECRET = "old-notify";
  process.env.ALERT_NOTIFY_SECRET_NEXT = "new-notify";
  try {
    assert.equal(verifySecret(request("old-notify")), true);
    assert.equal(verifySecret(request("new-notify")), true);
    assert.equal(verifySecret(request("wrong-notify")), false);
  } finally {
    [process.env.ALERT_NOTIFY_SECRET, process.env.ALERT_NOTIFY_SECRET_NEXT] = saved;
  }
});

test("publisher accepts effective old and next secrets during rotation", () => {
  const saved = [
    process.env.SATELLITE_PUBLISH_SECRET,
    process.env.SATELLITE_PUBLISH_SECRET_NEXT,
    process.env.BLOB_READ_WRITE_TOKEN,
  ];
  delete process.env.SATELLITE_PUBLISH_SECRET;
  process.env.BLOB_READ_WRITE_TOKEN = "old-publish";
  process.env.SATELLITE_PUBLISH_SECRET_NEXT = "new-publish";
  try {
    assert.equal(verifyPublishSecret(request("old-publish")), true);
    assert.equal(verifyPublishSecret(request("new-publish")), true);
    assert.equal(verifyPublishSecret(request("wrong-publish")), false);
  } finally {
    const names = ["SATELLITE_PUBLISH_SECRET", "SATELLITE_PUBLISH_SECRET_NEXT", "BLOB_READ_WRITE_TOKEN"];
    names.forEach((name, index) => {
      if (saved[index] == null) delete process.env[name];
      else process.env[name] = saved[index];
    });
  }
});
