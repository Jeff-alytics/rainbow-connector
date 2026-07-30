const SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
export const TURNSTILE_ACTION = "alert-signup";

export function turnstileSiteKey() {
  return String(process.env.TURNSTILE_SITE_KEY || "").trim();
}

function turnstileSecretKey() {
  return String(process.env.TURNSTILE_SECRET_KEY || "").trim();
}

export function configuredTurnstile() {
  return !!(turnstileSiteKey() && turnstileSecretKey());
}

function allowedHostnames() {
  return new Set(
    String(process.env.TURNSTILE_ALLOWED_HOSTNAMES || "therainbowconnector.com,www.therainbowconnector.com")
      .split(",")
      .map(value => value.trim().toLowerCase())
      .filter(Boolean),
  );
}

export async function verifyTurnstile(token, remoteIp, fetchImpl = fetch) {
  if (!configuredTurnstile() || !token || typeof token !== "string" || token.length > 2048) {
    return { ok: false, reason: "missing-or-invalid-token" };
  }
  try {
    const response = await fetchImpl(SITEVERIFY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        secret: turnstileSecretKey(),
        response: token,
        remoteip: remoteIp || undefined,
      }),
      signal: AbortSignal.timeout(8000),
    });
    const result = await response.json().catch(() => ({}));
    const hostname = String(result.hostname || "").toLowerCase();
    if (!response.ok || result.success !== true) return { ok: false, reason: "verification-failed" };
    if (result.action !== TURNSTILE_ACTION) return { ok: false, reason: "wrong-action" };
    if (!allowedHostnames().has(hostname)) return { ok: false, reason: "wrong-hostname" };
    return { ok: true, hostname };
  } catch {
    return { ok: false, reason: "verification-unavailable" };
  }
}
