import { saveCandidateHistory } from "./candidate-history-common.mjs";
import { saveGoEvents } from "./go-event-common.mjs";
import { capturePendingFaaEvidence } from "./go-evidence-common.mjs";
import { capturePendingNimsEvidence } from "./usgs-nims-common.mjs";
import { capturePendingWebcoosEvidence } from "./webcoos-common.mjs";
import { capturePendingAlertCaEvidence } from "./alertca-common.mjs";
import { strictGoCandidates } from "./artifact-policy.mjs";
import {
  loadFreshBlobSatelliteArtifact,
  loadFreshStoredSatelliteArtifact,
} from "./satellite-artifact-common.mjs";
import {
  SUBS_KEY,
  UNSUB_PREFIX,
  baseUrl,
  distKm,
  escapeHtml,
  json,
  readJsonBody,
  redis,
  redisPipeline,
  saveSub,
  sendEmail,
  verifySecret,
} from "./alert-common.mjs";

export const config = {
  maxDuration: 60,
};

export const ALERT_RADIUS_KM = 15;
const DEFAULT_COOLDOWN_HOURS = Number(process.env.ALERT_COOLDOWN_HOURS || 12);
const MAX_SENDS_PER_RUN = Number(process.env.ALERT_MAX_SENDS_PER_RUN || 25);

function wait(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export function nearestCandidate(sub, candidates, radiusKm = ALERT_RADIUS_KM) {
  let best = null;
  for (const c of candidates) {
    const d = distKm(sub.lat, sub.lon, c.lat, c.lon);
    if (d <= radiusKm && (!best || d < best.distanceKm)) best = { candidate: c, distanceKm: d };
  }
  return best;
}

async function loadFinalizedArtifact() {
  return await loadFreshStoredSatelliteArtifact()
    || await loadFreshBlobSatelliteArtifact();
}

export function alertableGoCandidates(artifact) {
  return strictGoCandidates(artifact);
}

const COMPASS_POINTS = [
  "north", "north-northeast", "northeast", "east-northeast",
  "east", "east-southeast", "southeast", "south-southeast",
  "south", "south-southwest", "southwest", "west-southwest",
  "west", "west-northwest", "northwest", "north-northwest",
];

export function lookInstructions(candidate) {
  const numeric = value => value == null || value === "" ? null : Number(value);
  const rawBearing = numeric(candidate?.direction?.bearing);
  const bearing = Number.isFinite(rawBearing) ? (rawBearing % 360 + 360) % 360 : null;
  const compass = bearing == null ? null : COMPASS_POINTS[Math.round(bearing / 22.5) % 16];
  const suppliedTop = numeric(candidate?.evidence?.rainbowArcDeg);
  const sunElevation = numeric(candidate?.evidence?.sunElevationDeg);
  const bowTop = Number.isFinite(suppliedTop)
    ? suppliedTop
    : Number.isFinite(sunElevation)
      ? Math.max(0, 42 - sunElevation)
      : null;
  const direction = compass
    ? `Face ${compass} (about ${Math.round(bearing)} degrees from north)`
    : "Face the sky opposite the sun";
  const height = bowTop == null
    ? "Scan upward from the horizon"
    : `Scan from the horizon up to about ${Math.round(bowTop)} degrees high`;
  return { bearing, compass, bowTop, direction, height };
}

function isCoolingDown(sub, candidate, nowMs, cooldownMs) {
  const last = sub.lastAlertAt ? new Date(sub.lastAlertAt).getTime() : 0;
  if (!last) return false;
  if (nowMs - last < cooldownMs) return true;
  return sub.lastAlertKey === candidate.id && nowMs - last < cooldownMs * 2;
}

function alertEmail({ req, sub, candidate, distanceKm, artifact }) {
  const generated = artifact.generatedAt ? new Date(artifact.generatedAt) : new Date();
  const when = generated.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  const label = candidate.label || candidate.nearestZip?.name || `${candidate.lat.toFixed(2)}, ${candidate.lon.toFixed(2)}`;
  const look = lookInstructions(candidate);
  const site = baseUrl(req);
  const unsubscribe = `${site}/api/alerts?unsubscribe=${encodeURIComponent(sub.unsubscribeToken)}`;
  const isNew = candidate.persistence?.confirmed !== true;
  const subject = `Rainbow GO — look now near ${sub.zip}`;
  const text = `${isNew ? "New rainbow GO signal" : "Rainbow GO candidate"} near ${label}.

LOOK NOW: ${look.direction}. ${look.height}. If you can see the sun, put it behind you.

For ${sub.place} ${sub.zip}, the detector found rain, low sun, and enough light about ${Math.round(distanceKm)} km away at ${when}.
${isNew ? "This is a fresh first detection; rainbow opportunities can be brief." : ""}

Open the map: ${site}
Unsubscribe: ${unsubscribe}`;
  const body = `<p><strong>${isNew ? "New rainbow GO signal" : "Rainbow GO candidate"} near ${escapeHtml(label)}</strong></p>
<p style="font-size:18px"><strong>Where to look:</strong> ${escapeHtml(look.direction)}.</p>
<p><strong>How high:</strong> ${escapeHtml(look.height)}. If you can see the sun, put it behind you.</p>
<p>The detector found rain, low sun, and enough light about ${Math.round(distanceKm)} km from your watched ZIP at ${escapeHtml(when)}.</p>
${isNew ? "<p><strong>Look now:</strong> this is a fresh first detection, and rainbow opportunities can be brief.</p>" : ""}
<p><a href="${escapeHtml(site)}" style="display:inline-block;background:#4968d8;color:#fff;text-decoration:none;padding:10px 14px;border-radius:999px">Open the map</a></p>
<p style="color:#667;font-size:12px">Fun forecast only. Local clouds, terrain, and your exact view still matter.</p>
<p style="color:#667;font-size:12px"><a href="${escapeHtml(unsubscribe)}">Unsubscribe</a></p>`;
  return { to: sub.email, subject, text, html: body };
}

async function loadActiveSubscriptions() {
  const keys = await redis(["SMEMBERS", SUBS_KEY]);
  if (!Array.isArray(keys) || !keys.length) return [];
  const responses = await redisPipeline(keys.map(key => ["GET", key]), { allowCommandErrors: true });
  return responses
    .map((r, idx) => {
      if (!r?.result) return null;
      try {
        const sub = JSON.parse(r.result);
        sub._key = keys[idx];
        return sub;
      } catch {
        return null;
      }
    })
    .filter(sub => sub?.active && Number.isFinite(sub.lat) && Number.isFinite(sub.lon) && sub.email && sub.zip);
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    json(res, 405, { ok: false, error: "Method not allowed." });
    return;
  }

  const body = await readJsonBody(req);
  if (!verifySecret(req, body)) {
    json(res, 401, { ok: false, error: "Unauthorized." });
    return;
  }

  const started = Date.now();
  const dryRun = !!body.dryRun;
  const radiusKm = ALERT_RADIUS_KM;
  const cooldownMs = Number(body.cooldownHours || DEFAULT_COOLDOWN_HOURS) * 60 * 60 * 1000;
  const maxSends = Math.max(0, Math.min(Number(body.maxSends || MAX_SENDS_PER_RUN), MAX_SENDS_PER_RUN));

  const [artifact, subs] = await Promise.all([
    loadFinalizedArtifact(),
    loadActiveSubscriptions(),
  ]);

  if (!artifact) {
    json(res, 503, {
      ok: false,
      error: "No fresh finalized candidate scan is available.",
    });
    return;
  }

  const collection = await Promise.allSettled([
    saveCandidateHistory(artifact),
    saveGoEvents(artifact),
  ]);
  for (const result of collection) {
    if (result.status === "rejected") console.warn("[candidate-collection] save failed:", result.reason?.message || result.reason);
  }

  const candidates = alertableGoCandidates(artifact);
  const nowMs = Date.now();
  const matches = [];
  let skippedCooldown = 0;

  for (const sub of subs) {
    const match = nearestCandidate(sub, candidates, radiusKm);
    if (!match) continue;
    if (isCoolingDown(sub, match.candidate, nowMs, cooldownMs)) {
      skippedCooldown++;
      continue;
    }
    matches.push({ sub, ...match });
  }

  let sent = 0;
  const errors = [];
  for (const match of matches.slice(0, maxSends)) {
    try {
      if (!dryRun) {
        await sendEmail(alertEmail({ req, artifact, ...match }));
        match.sub.lastAlertAt = new Date().toISOString();
        match.sub.lastAlertKey = match.candidate.id || artifact.generatedAt || "go";
        match.sub.updatedAt = match.sub.lastAlertAt;
        await saveSub(match.sub);
        if (match.sub.unsubscribeToken) await redis(["SET", UNSUB_PREFIX + match.sub.unsubscribeToken, match.sub._key]);
        await wait(650);
      }
      sent++;
    } catch (err) {
      errors.push({ email: match.sub.email, error: err.message || "send failed" });
    }
  }

  // Review imagery is supporting research work. Capture it only after every
  // user-facing alert has been sent so multi-camera collection cannot delay email.
  const faaEvidence = await capturePendingFaaEvidence(2).catch(error => [
    { stored: false, reason: error?.message || "capture_failed" },
  ]);
  const nimsEvidence = await capturePendingNimsEvidence(2).catch(error => [
    { stored: false, reason: error?.message || "nims_capture_failed" },
  ]);
  const webcoosEvidence = await capturePendingWebcoosEvidence(2).catch(error => [
    { stored: false, reason: error?.message || "webcoos_capture_failed" },
  ]);
  const alertCaEvidence = await capturePendingAlertCaEvidence(2).catch(error => [
    { stored: false, reason: error?.message || "alertca_capture_failed" },
  ]);
  const evidenceCapture = [...faaEvidence, ...nimsEvidence, ...webcoosEvidence, ...alertCaEvidence];

  json(res, 200, {
    ok: true,
    dryRun,
    generatedAt: artifact.generatedAt,
    candidates: candidates.length,
    subscriptions: subs.length,
    matches: matches.length,
    skippedCooldown,
    sent,
    limited: matches.length > maxSends,
    runtimeMs: Date.now() - started,
    errors,
    collection: {
      history: collection[0].status === "fulfilled" ? collection[0].value : { stored: false, reason: "error" },
      goEvents: collection[1].status === "fulfilled" ? collection[1].value : { stored: false, reason: "error" },
      evidence: evidenceCapture,
    },
  });
}
