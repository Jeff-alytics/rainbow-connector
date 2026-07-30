import { json, redis, redisPipeline, SUBS_KEY } from "./alert-common.mjs";
import { hasReviewSession } from "./review-auth-common.mjs";

export const config = { maxDuration: 10 };

export function sanitizeSubscribers(rows) {
  return (rows || [])
    .filter(sub => sub?.active && sub.email && sub.zip)
    .map(sub => ({
      email: String(sub.email),
      zip: String(sub.zip),
      place: sub.place || [sub.city, sub.state].filter(Boolean).join(", ") || null,
      confirmedAt: sub.confirmedAt || sub.createdAt || null,
      lastAlertAt: sub.lastAlertAt || null,
    }))
    .sort((a, b) => new Date(b.confirmedAt || 0) - new Date(a.confirmedAt || 0));
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    json(res, 405, { ok: false, error: "Method not allowed." });
    return;
  }
  if (!hasReviewSession(req)) {
    json(res, 401, { ok: false, error: "Unauthorized." });
    return;
  }
  const keys = await redis(["SMEMBERS", SUBS_KEY]);
  if (!Array.isArray(keys) || !keys.length) {
    json(res, 200, { ok: true, subscribers: 0, items: [] });
    return;
  }
  const responses = await redisPipeline(keys.slice(0, 1000).map(key => ["GET", key]));
  const rows = responses.map(response => {
    try { return response?.result ? JSON.parse(response.result) : null; } catch { return null; }
  }).filter(Boolean);
  const items = sanitizeSubscribers(rows);
  json(res, 200, { ok: true, subscribers: items.length, items });
}
