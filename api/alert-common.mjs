import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");

export const SUBS_KEY = "rainbow:alert:subs";
export const SUB_PREFIX = "rainbow:alert:sub:";
export const CONFIRM_PREFIX = "rainbow:alert:confirm:";
export const UNSUB_PREFIX = "rainbow:alert:unsub:";

let zipCache = null;

export function json(res, status, data) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.status(status).send(JSON.stringify(data));
}

export function html(res, status, body) {
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.status(status).send(`<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Rainbow Connector</title>
<body style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:38rem;margin:12vh auto;padding:0 1.25rem;line-height:1.5;color:#223">
${body}
</body></html>`);
}

export async function readJsonBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  if (typeof req.body === "string") {
    try { return JSON.parse(req.body); } catch { return {}; }
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { return {}; }
}

export function normalizeEmail(value) {
  return String(value || "").trim().toLowerCase();
}

export function normalizeZip(value) {
  const match = String(value || "").match(/\d{5}/);
  return match ? match[0] : "";
}

export function validEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) && email.length <= 254;
}

export function sha(value) {
  return createHash("sha256").update(String(value)).digest("hex");
}

export function subKey(email) {
  return SUB_PREFIX + sha(normalizeEmail(email));
}

export function makeToken() {
  return randomBytes(24).toString("base64url");
}

export function configuredStore() {
  return !!(redisUrl() && redisToken());
}

export function configuredResend() {
  return !!String(process.env.RESEND_API_KEY || "").trim();
}

export function baseUrl(req) {
  const envUrl = String(process.env.ALERT_BASE_URL || process.env.VERCEL_PROJECT_PRODUCTION_URL || "").trim();
  if (envUrl) return envUrl.startsWith("http") ? envUrl.replace(/\/$/, "") : `https://${envUrl}`;
  const host = req.headers["x-forwarded-host"] || req.headers.host || "therainbowconnector.com";
  const proto = req.headers["x-forwarded-proto"] || "https";
  return `${proto}://${host}`.replace(/\/$/, "");
}

export async function loadZips() {
  if (!zipCache) {
    zipCache = JSON.parse(await readFile(path.join(ROOT, "zip-centroids.json"), "utf8"));
  }
  return zipCache;
}

export async function lookupZip(zip) {
  const zips = await loadZips();
  const row = zips[zip];
  if (!row) return null;
  return { zip, city: row[0], state: row[1], lat: row[2], lon: row[3], place: `${row[0]}, ${row[1]}` };
}

function redisUrl() {
  return String(process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL || "")
    .trim()
    .replace(/\/$/, "");
}

function redisToken() {
  return String(process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN || "").trim();
}

export async function redis(command) {
  if (!configuredStore()) throw new Error("alert storage is not configured");
  const response = await fetch(redisUrl(), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${redisToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(command),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) throw new Error(data.error || `Redis command failed (${response.status})`);
  return data.result;
}

export async function redisPipeline(commands, options = {}) {
  if (!commands.length) return [];
  if (!configuredStore()) throw new Error("alert storage is not configured");
  const response = await fetch(`${redisUrl()}/pipeline`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${redisToken()}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(commands),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) throw new Error(data.error || `Redis pipeline failed (${response.status})`);
  if (Array.isArray(data) && !options.allowCommandErrors) {
    const failed = data.find(item => item?.error);
    if (failed) throw new Error(failed.error || "Redis pipeline command failed");
  }
  return data;
}

export async function getSub(email) {
  const raw = await redis(["GET", subKey(email)]);
  return raw ? JSON.parse(raw) : null;
}

export async function saveSub(sub) {
  const key = subKey(sub.email);
  const stored = { ...sub };
  delete stored._key;
  await redis(["SET", key, JSON.stringify(stored)]);
  if (sub.active) await redis(["SADD", SUBS_KEY, key]);
  else await redis(["SREM", SUBS_KEY, key]);
  return key;
}

export async function sendEmail({ to, subject, text, html }) {
  if (!configuredResend()) throw new Error("RESEND_API_KEY is not configured");
  const apiKey = String(process.env.RESEND_API_KEY || "").trim();
  const from = String(process.env.RESEND_FROM || "The Rainbow Connector <alerts@therainbowconnector.com>").trim();
  const response = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ from, to, subject, text, html }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.error) {
    const message = data?.error?.message || data?.message || `Resend send failed (${response.status})`;
    throw new Error(message);
  }
  return data;
}

export function verifySecret(req, body = {}) {
  const expected = [
    process.env.ALERT_NOTIFY_SECRET,
    process.env.ALERT_NOTIFY_SECRET_NEXT,
  ].map(value => String(value || "").trim()).filter(Boolean);
  if (!expected.length) return false;
  const auth = String(req.headers.authorization || "");
  const given = String(body.secret || req.query?.secret || auth.replace(/^Bearer\s+/i, ""));
  if (!given) return false;
  const supplied = Buffer.from(given);
  return expected.some(value => {
    const configured = Buffer.from(value);
    return configured.length === supplied.length && timingSafeEqual(configured, supplied);
  });
}

export function distKm(la1, lo1, la2, lo2) {
  const rad = Math.PI / 180;
  const R = 6371;
  const dLa = (la2 - la1) * rad;
  const dLo = (lo2 - lo1) * rad;
  const a = Math.sin(dLa / 2) ** 2 + Math.cos(la1 * rad) * Math.cos(la2 * rad) * Math.sin(dLo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
