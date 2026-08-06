import { createHash } from "node:crypto";
import { distKm } from "./alert-common.mjs";

export const MANUAL_RAINBOW_INDEX = "rainbow:manual:rainbows";
export const MANUAL_RAINBOW_PREFIX = "rainbow:manual:rainbow:";
export const MANUAL_MATCH_WINDOW_MINUTES = 30;

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function normalizeManualRainbow(input, now = new Date()) {
  const sourceUrl = String(input?.sourceUrl || "").trim();
  let parsedUrl;
  try { parsedUrl = new URL(sourceUrl); } catch { throw new Error("Enter a valid public source URL."); }
  if (!['http:', 'https:'].includes(parsedUrl.protocol)) throw new Error("Source URL must use HTTP or HTTPS.");
  const observedAt = new Date(input?.observedAt || "");
  if (!Number.isFinite(observedAt.getTime())) throw new Error("Enter a valid observation time.");
  if (observedAt.getTime() > now.getTime() + 10 * 60_000) throw new Error("Observation time cannot be in the future.");
  const lat = finite(input?.lat), lon = finite(input?.lon);
  if (lat == null || lat < -90 || lat > 90 || lon == null || lon < -180 || lon > 180) {
    throw new Error("Enter valid latitude and longitude.");
  }
  const matchRadiusKm = Math.max(1, Math.min(finite(input?.matchRadiusKm) ?? 25, 100));
  const certainty = ["exact", "approximate"].includes(input?.locationCertainty)
    ? input.locationCertainty : "approximate";
  const seed = `${parsedUrl.href}|${observedAt.toISOString()}|${lat.toFixed(5)}|${lon.toFixed(5)}`;
  return {
    id: `manual-rainbow-${createHash("sha256").update(seed).digest("hex").slice(0, 20)}`,
    sourceUrl: parsedUrl.href,
    observedAt: observedAt.toISOString(), lat, lon, matchRadiusKm,
    locationLabel: String(input?.locationLabel || "").trim().slice(0, 160) || null,
    locationCertainty: certainty,
    evidenceLabel: input?.evidenceLabel === "possible" ? "possible" : "rainbow",
    notes: String(input?.notes || "").trim().slice(0, 1000) || null,
  };
}

function eventPoint(event) {
  return event?.latestLocation || event?.representative || null;
}

function eventTime(event) {
  return new Date(event?.lastSeenAt || event?.representative?.detectedAt || event?.firstSeenAt || 0).getTime();
}

function lane(event) {
  if (event?.researchSource === "v4_shadow") return "v4";
  if (event?.researchSource === "v5_shadow") return "v5";
  if (event?.candidateType !== "research_possible") return "operational";
  return null;
}

function modelDecision(event, modelLane) {
  if (modelLane === "operational") {
    if (event?.candidateClass === "GO" || event?.candidateType === "live_go") return "GO";
    return "POSSIBLE";
  }
  if (modelLane === "v4") {
    const prediction = (event?.v4ShadowPredictions || []).at(-1) || {};
    if (prediction.lane === "go" || prediction.lane === "primary"
      || String(prediction.classification || "").startsWith("GO")) return "GO";
    return "POSSIBLE";
  }
  return "POSSIBLE";
}

export function matchManualRainbow(manual, events) {
  const observedMs = new Date(manual.observedAt).getTime();
  const best = { operational: null, v4: null, v5: null };
  for (const event of events || []) {
    const modelLane = lane(event), point = eventPoint(event), at = eventTime(event);
    if (!modelLane || !Number.isFinite(point?.lat) || !Number.isFinite(point?.lon) || !at) continue;
    const timeGapMinutes = Math.abs(at - observedMs) / 60_000;
    if (timeGapMinutes > MANUAL_MATCH_WINDOW_MINUTES) continue;
    const distanceKm = distKm(manual.lat, manual.lon, point.lat, point.lon);
    const candidate = {
      eventId: event.id, distanceKm: Number(distanceKm.toFixed(1)),
      timeGapMinutes: Number(timeGapMinutes.toFixed(1)),
      candidateClass: event.candidateClass || null,
      modelDecision: modelDecision(event, modelLane),
      score: Number.isFinite(event.peakScore) ? event.peakScore : null,
      matched: distanceKm <= manual.matchRadiusKm,
    };
    const current = best[modelLane];
    if (!current || candidate.distanceKm < current.distanceKm
      || (candidate.distanceKm === current.distanceKm && candidate.timeGapMinutes < current.timeGapMinutes)) {
      best[modelLane] = candidate;
    }
  }
  return {
    ...best,
    exactReplayStatus: "pending",
    coverageNote: "V4/V5 matches use recorded Review projections until an exact frozen-artifact replay is completed.",
  };
}
