import {
  CONFIRM_PREFIX,
  UNSUB_PREFIX,
  baseUrl,
  configuredResend,
  configuredStore,
  escapeHtml,
  getSub,
  html,
  json,
  lookupZip,
  makeToken,
  normalizeEmail,
  normalizeZip,
  readJsonBody,
  redis,
  saveSub,
  sendEmail,
  subKey,
  validEmail,
} from "./alert-common.mjs";
import { configuredTurnstile, turnstileSiteKey, verifyTurnstile } from "./turnstile.mjs";

const CONFIRM_TTL_SECONDS = Number(process.env.ALERT_CONFIRM_TTL_SECONDS || 3 * 24 * 60 * 60);

function clientIp(req) {
  return String(req.headers["x-forwarded-for"] || req.socket?.remoteAddress || "unknown").split(",")[0].trim();
}

async function rateLimit(scope, limit, seconds) {
  const key = `rainbow:alert:rl:${scope}`;
  const n = await redis(["INCR", key]);
  if (n === 1) await redis(["EXPIRE", key, seconds]);
  return n <= limit;
}

function confirmationEmail({ email, place, zip, confirmUrl }) {
  const safePlace = escapeHtml(place);
  const safeZip = escapeHtml(zip);
  const text = `Confirm rainbow alerts for ${safePlace} ${safeZip}: ${confirmUrl}

You will only get an email when the detector finds a GO candidate near that ZIP.`;
  const body = `<p>Confirm rainbow alerts for <strong>${safePlace} ${safeZip}</strong>.</p>
<p><a href="${escapeHtml(confirmUrl)}" style="display:inline-block;background:#4968d8;color:#fff;text-decoration:none;padding:10px 14px;border-radius:999px">Confirm alerts</a></p>
<p style="color:#667;font-size:13px">You will only get an email when the detector finds a GO candidate near that ZIP.</p>`;
  return { to: email, subject: `Confirm rainbow alerts for ${zip}`, text, html: body };
}

async function handleSignup(req, res) {
  if (!configuredStore() || !configuredResend() || !configuredTurnstile()) {
    json(res, 501, { ok: false, error: "Email alerts are not configured yet." });
    return;
  }

  const body = await readJsonBody(req);
  const email = normalizeEmail(body.email);
  const zip = normalizeZip(body.zip);
  if (!validEmail(email)) {
    json(res, 400, { ok: false, error: "Enter a valid email address." });
    return;
  }
  const place = await lookupZip(zip);
  if (!place) {
    json(res, 400, { ok: false, error: "Enter a valid US ZIP code." });
    return;
  }

  const ipOk = await rateLimit(`ip:${clientIp(req)}`, 12, 60 * 60);
  const emailOk = await rateLimit(`email:${email}`, 4, 60 * 60);
  if (!ipOk || !emailOk) {
    json(res, 429, { ok: false, error: "Too many alert requests. Try again later." });
    return;
  }

  const turnstile = await verifyTurnstile(
    String(body.turnstileToken || body["cf-turnstile-response"] || ""),
    clientIp(req),
  );
  if (!turnstile.ok) {
    json(res, 400, { ok: false, error: "Complete the bot check and try again." });
    return;
  }

  const current = await getSub(email).catch(() => null);
  if (current?.active && current.zip === zip) {
    json(res, 200, { ok: true, status: "active", message: `You're already signed up for ${place.place} ${zip}.` });
    return;
  }

  const token = makeToken();
  const confirmUrl = `${baseUrl(req)}/api/alerts?token=${encodeURIComponent(token)}`;
  const pending = {
    email,
    zip,
    city: place.city,
    state: place.state,
    place: place.place,
    lat: place.lat,
    lon: place.lon,
    createdAt: new Date().toISOString(),
    previousCreatedAt: current?.createdAt || null,
    previousUnsubscribeToken: current?.unsubscribeToken || null,
  };

  await redis(["SET", CONFIRM_PREFIX + token, JSON.stringify(pending), "EX", CONFIRM_TTL_SECONDS]);
  try {
    await sendEmail(confirmationEmail({ email, place: place.place, zip, confirmUrl }));
  } catch (err) {
    await redis(["DEL", CONFIRM_PREFIX + token]).catch(() => null);
    json(res, 502, { ok: false, error: err.message || "Could not send confirmation email." });
    return;
  }

  json(res, 200, { ok: true, status: "pending", message: `Check ${email} to confirm alerts for ${place.place} ${zip}.` });
}

async function handleConfirm(req, res) {
  const token = String(req.query?.token || "");
  const raw = token ? await redis(["GET", CONFIRM_PREFIX + token]).catch(() => null) : null;
  if (!raw) {
    html(res, 404, `<h1>Alert link expired</h1><p>This confirmation link is no longer valid. You can sign up again at <a href="/">The Rainbow Connector</a>.</p>`);
    return;
  }

  const pending = JSON.parse(raw);
  const existing = await getSub(pending.email).catch(() => null);
  const unsubscribeToken = existing?.unsubscribeToken || pending.previousUnsubscribeToken || makeToken();
  const now = new Date().toISOString();
  const sub = {
    email: pending.email,
    zip: pending.zip,
    city: pending.city,
    state: pending.state,
    place: pending.place,
    lat: pending.lat,
    lon: pending.lon,
    active: true,
    createdAt: existing?.createdAt || pending.previousCreatedAt || now,
    updatedAt: now,
    confirmedAt: now,
    unsubscribeToken,
    lastAlertAt: existing?.lastAlertAt || null,
    lastAlertKey: existing?.lastAlertKey || null,
  };
  const key = await saveSub(sub);
  await redis(["SET", UNSUB_PREFIX + unsubscribeToken, key]);
  await redis(["DEL", CONFIRM_PREFIX + token]);

  html(res, 200, `<h1>Alerts confirmed</h1><p>You are signed up for rainbow alerts near <strong>${escapeHtml(sub.place)} ${escapeHtml(sub.zip)}</strong>.</p><p><a href="/">Back to The Rainbow Connector</a></p>`);
}

async function handleUnsubscribe(req, res) {
  const token = String(req.query?.unsubscribe || "");
  const key = token ? await redis(["GET", UNSUB_PREFIX + token]).catch(() => null) : null;
  if (!key) {
    html(res, 404, `<h1>Unsubscribe link not found</h1><p>This unsubscribe link is no longer valid.</p>`);
    return;
  }
  const raw = await redis(["GET", key]).catch(() => null);
  if (raw) {
    const sub = JSON.parse(raw);
    sub.active = false;
    sub.updatedAt = new Date().toISOString();
    await saveSub(sub);
  }
  await redis(["DEL", UNSUB_PREFIX + token]);
  html(res, 200, `<h1>Alerts stopped</h1><p>You will not receive more Rainbow Connector alerts at that address.</p><p><a href="/">Back to The Rainbow Connector</a></p>`);
}

export default async function handler(req, res) {
  if (req.method === "GET" && req.query?.status) {
    json(res, 200, {
      ok: true,
      configured: configuredStore() && configuredResend() && configuredTurnstile(),
      storeConfigured: configuredStore(),
      emailConfigured: configuredResend(),
      captchaConfigured: configuredTurnstile(),
      turnstileSiteKey: configuredTurnstile() ? turnstileSiteKey() : null,
    });
    return;
  }
  if (req.method === "POST") return handleSignup(req, res);
  if (req.method === "GET" && req.query?.token) return handleConfirm(req, res);
  if (req.method === "GET" && req.query?.unsubscribe) return handleUnsubscribe(req, res);
  res.setHeader("Allow", "POST, GET");
  json(res, 405, { ok: false, error: "Method not allowed." });
}
