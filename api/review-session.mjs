import { timingSafeEqual } from "node:crypto";
import { json, readJsonBody } from "./alert-common.mjs";
import {
  clearReviewCookie,
  hasReviewSession,
  makeReviewToken,
  reviewAuthConfigured,
  reviewCookie,
} from "./review-auth-common.mjs";

export const config = { maxDuration: 10 };

function validPassword(given) {
  const expected = String(process.env.GO_REVIEW_PASSWORD || "").trim();
  const value = String(given || "");
  if (!expected || !value) return false;
  const a = Buffer.from(expected), b = Buffer.from(value);
  return a.length === b.length && timingSafeEqual(a, b);
}

export default async function handler(req, res) {
  res.setHeader("Cache-Control", "no-store");
  if (req.method === "GET") {
    const configured = reviewAuthConfigured() && !!String(process.env.GO_REVIEW_PASSWORD || "").trim();
    json(res, 200, { ok: true, configured, authenticated: configured && hasReviewSession(req) });
    return;
  }
  if (req.method === "POST") {
    const body = await readJsonBody(req);
    if (!validPassword(body.password)) {
      json(res, 401, { ok: false, error: "Incorrect review password." });
      return;
    }
    res.setHeader("Set-Cookie", reviewCookie(makeReviewToken()));
    json(res, 200, { ok: true, authenticated: true });
    return;
  }
  if (req.method === "DELETE") {
    res.setHeader("Set-Cookie", clearReviewCookie());
    json(res, 200, { ok: true, authenticated: false });
    return;
  }
  res.setHeader("Allow", "GET, POST, DELETE");
  json(res, 405, { ok: false, error: "Method not allowed." });
}
