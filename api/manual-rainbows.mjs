import { configuredStore, json, readJsonBody, redis, redisPipeline } from "./alert-common.mjs";
import { loadGoEventsBetween } from "./go-event-common.mjs";
import { MANUAL_MATCH_WINDOW_MINUTES, MANUAL_RAINBOW_INDEX, MANUAL_RAINBOW_PREFIX,
  matchManualRainbow, normalizeManualRainbow } from "./manual-rainbow-common.mjs";
import { hasReviewSession } from "./review-auth-common.mjs";

export const config = { maxDuration: 10 };

async function loadManualRainbows(limit = 250) {
  const count = Math.max(1, Math.min(Number(limit) || 250, 1000));
  const ids = await redis(["ZREVRANGE", MANUAL_RAINBOW_INDEX, 0, count - 1]);
  if (!ids?.length) return [];
  const rows = [];
  for (let offset = 0; offset < ids.length; offset += 40) {
    const batch = await redis(["MGET", ...ids.slice(offset, offset + 40).map(id => MANUAL_RAINBOW_PREFIX + id)]);
    rows.push(...(batch || []));
  }
  return rows.map(row => { try { return row ? JSON.parse(row) : null; } catch { return null; } }).filter(Boolean);
}

export default async function handler(req, res) {
  if (!hasReviewSession(req)) { json(res, 401, { ok: false, error: "Unauthorized." }); return; }
  if (!configuredStore()) { json(res, 503, { ok: false, error: "Manual validation store is unavailable." }); return; }
  if (req.method === "GET") {
    const items = await loadManualRainbows(req.query?.limit);
    json(res, 200, { ok: true, events: items.length, items });
    return;
  }
  if (req.method === "POST") {
    try {
      const manual = normalizeManualRainbow(await readJsonBody(req));
      const observedMs = new Date(manual.observedAt).getTime();
      const windowMs = MANUAL_MATCH_WINDOW_MINUTES * 60_000;
      const events = await loadGoEventsBetween(observedMs - windowMs, observedMs + windowMs, 1000);
      const saved = { ...manual, createdAt: new Date().toISOString(), matches: matchManualRainbow(manual, events) };
      await redisPipeline([
        ["SET", MANUAL_RAINBOW_PREFIX + saved.id, JSON.stringify(saved)],
        ["ZADD", MANUAL_RAINBOW_INDEX, observedMs, saved.id],
      ]);
      json(res, 201, { ok: true, item: saved });
    } catch (error) {
      json(res, 400, { ok: false, error: error?.message || "Could not save manual rainbow." });
    }
    return;
  }
  res.setHeader("Allow", "GET, POST");
  json(res, 405, { ok: false, error: "Method not allowed." });
}
