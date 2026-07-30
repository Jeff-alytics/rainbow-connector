import { createHmac, timingSafeEqual } from "node:crypto";

export const REVIEW_COOKIE = "rainbow_review";
const SESSION_SECONDS = 7 * 24 * 60 * 60;

function secret() {
  return String(process.env.GO_REVIEW_SECRET || "").trim();
}

function signature(payload) {
  return createHmac("sha256", secret()).update(payload).digest("base64url");
}

export function reviewAuthConfigured() {
  return !!secret();
}

export function makeReviewToken(nowMs = Date.now()) {
  if (!reviewAuthConfigured()) throw new Error("Review authentication is not configured.");
  const payload = Buffer.from(JSON.stringify({ exp: nowMs + SESSION_SECONDS * 1000 })).toString("base64url");
  return `${payload}.${signature(payload)}`;
}

export function validReviewToken(token, nowMs = Date.now()) {
  if (!reviewAuthConfigured() || !token) return false;
  const [payload, given] = String(token).split(".");
  if (!payload || !given) return false;
  const expected = signature(payload);
  const a = Buffer.from(expected), b = Buffer.from(given);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return false;
  try {
    const data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return Number(data.exp) > nowMs;
  } catch {
    return false;
  }
}

export function reviewTokenFromRequest(req) {
  const cookies = String(req.headers?.cookie || "").split(";");
  for (const item of cookies) {
    const [name, ...rest] = item.trim().split("=");
    if (name === REVIEW_COOKIE) return decodeURIComponent(rest.join("="));
  }
  return "";
}

export function hasReviewSession(req) {
  return validReviewToken(reviewTokenFromRequest(req));
}

export function reviewCookie(token) {
  return `${REVIEW_COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_SECONDS}`;
}

export function clearReviewCookie() {
  return `${REVIEW_COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;
}
