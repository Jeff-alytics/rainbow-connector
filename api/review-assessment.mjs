import { timingSafeEqual } from "node:crypto";
import { json, readJsonBody } from "./alert-common.mjs";
import { attachReviewAssessments } from "./go-event-common.mjs";

export const config = { maxDuration: 10 };
const MAX_ASSESSMENT_BODY_BYTES = 128 * 1024;

export function verifyReviewSecret(req) {
  const expected = String(process.env.REVIEW_ENRICH_SECRET || "").trim();
  const supplied = String(req.headers.authorization || "").replace(/^Bearer\s+/i, "").trim();
  if (!expected || !supplied) return false;
  const a = Buffer.from(expected), b = Buffer.from(supplied);
  return a.length === b.length && timingSafeEqual(a, b);
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    json(res, 405, { ok: false, error: "Method not allowed." });
    return;
  }
  if (!verifyReviewSecret(req)) { json(res, 401, { ok: false, error: "Unauthorized." }); return; }
  const statedSize = Number(req.headers["content-length"] || 0);
  if (statedSize > MAX_ASSESSMENT_BODY_BYTES) { json(res, 413, { ok: false, error: "Payload too large." }); return; }
  const body = await readJsonBody(req);
  if (Buffer.byteLength(JSON.stringify(body)) > MAX_ASSESSMENT_BODY_BYTES) {
    json(res, 413, { ok: false, error: "Assessment payload too large." }); return;
  }
  if (body?.schemaVersion !== "review-assessment.v1" || !Array.isArray(body?.assessments)) {
    json(res, 400, { ok: false, error: "Invalid assessment envelope." }); return;
  }
  if (body.assessments.length > 32) {
    json(res, 413, { ok: false, error: "Too many assessments in one batch." }); return;
  }
  const result = await attachReviewAssessments(body.assessments);
  json(res, 200, { ok: true, ...result });
}
