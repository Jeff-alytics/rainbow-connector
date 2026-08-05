import { json } from "./alert-common.mjs";
import { hasReviewSession } from "./review-auth-common.mjs";
import { loadV5CandidateDays } from "./v5-candidate-common.mjs";

export const config = { maxDuration: 10 };

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
  const days = await loadV5CandidateDays({ days: req.query?.days });
  json(res, 200, {
    ok: true,
    days: days.length,
    families: days.reduce((sum, day) => sum + day.items.length, 0),
    acquisitionEnabled: false,
    items: days,
  });
}
