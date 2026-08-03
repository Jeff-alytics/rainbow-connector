import { appendFile, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { configuredStore, redis, redisPipeline } from "./alert-common.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");

export const HISTORY_KEY = "rainbow:candidate:history";
const HISTORY_SEEN_PREFIX = "rainbow:candidate:history:seen:";
const LOCAL_HISTORY_PATH = path.join(ROOT, ".candidate-history.jsonl");

function compactGoes(goes) {
  if (!goes) return null;
  return {
    decision: goes.decision || null,
    positiveSources: Array.isArray(goes.positiveSources) ? goes.positiveSources : [],
    negativeSources: Array.isArray(goes.negativeSources) ? goes.negativeSources : [],
  };
}

function maxRuns() {
  const n = Number(process.env.CANDIDATE_HISTORY_MAX_RUNS || 288);
  return Math.max(12, Math.min(Number.isFinite(n) ? n : 288, 2016));
}

function compactCandidate(c) {
  return {
    id: c.id || null,
    rank: c.rank ?? null,
    lat: c.lat,
    lon: c.lon,
    label: c.label || c.nearestZip?.name || null,
    nearestZip: c.nearestZip || null,
    verdict: c.verdict || "go",
    confidence: c.confidence || (c.verdict === "watch" ? "possible" : "high"),
    direction: c.direction || null,
    persistence: c.persistence ? {
      confirmed: c.persistence.confirmed === true,
      scanCount: c.persistence.scanCount ?? null,
      firstSeenAt: c.persistence.firstSeenAt || null,
      lastSeenAt: c.persistence.lastSeenAt || null,
    } : null,
    evidence: c.evidence ? {
      sunElevationDeg: c.evidence.sunElevationDeg ?? null,
      rainbowArcDeg: c.evidence.rainbowArcDeg ?? null,
      directNormalIrradianceWm2: c.evidence.directNormalIrradianceWm2 ?? null,
      directRadiationWm2: c.evidence.directRadiationWm2 ?? null,
      cloudCoverPct: c.evidence.cloudCoverPct ?? null,
      rainPointDirectNormalIrradianceWm2: c.evidence.rainPointDirectNormalIrradianceWm2 ?? null,
      rainPointDirectRadiationWm2: c.evidence.rainPointDirectRadiationWm2 ?? null,
      rainPointCloudCoverPct: c.evidence.rainPointCloudCoverPct ?? null,
      rainIntensity: c.evidence.rainIntensity ?? null,
      observerRainIntensity: c.evidence.observerRainIntensity ?? null,
      rainPoint: c.evidence.rainPoint || null,
      score: c.evidence.score ?? null,
      selectionReason: c.evidence.selectionReason || null,
      goes: compactGoes(c.evidence.goes),
    } : null,
  };
}

export function compactHistoryRecord(artifact, meta = {}) {
  return {
    generatedAt: artifact.generatedAt || new Date().toISOString(),
    expiresAt: artifact.expiresAt || null,
    stale: !!artifact.stale,
    source: artifact.source || null,
    runtimeMs: artifact.runtimeMs ?? meta.runtimeMs ?? null,
    candidates: Array.isArray(artifact.candidates) ? artifact.candidates.map(compactCandidate) : [],
    possibleCandidates: Array.isArray(artifact.possibleCandidates) ? artifact.possibleCandidates.map(compactCandidate) : [],
    diagnostics: artifact.diagnostics ? {
      meshPoints: artifact.diagnostics.meshPoints ?? null,
      landPoints: artifact.diagnostics.landPoints ?? null,
      geometryMatches: artifact.diagnostics.geometryMatches ?? null,
      radarMatches: artifact.diagnostics.radarMatches ?? null,
      clusteredForVerification: artifact.diagnostics.clusteredForVerification ?? null,
      weatherApiCalls: artifact.diagnostics.weatherApiCalls ?? null,
      rainViewerTileRequests: artifact.diagnostics.rainViewerTileRequests ?? null,
      goCandidates: artifact.diagnostics.goCandidates ?? null,
      possibleCandidates: artifact.diagnostics.possibleCandidates ?? null,
      spatialSupportMethodVersion: artifact.diagnostics.spatialSupportMethodVersion || null,
      spatialSupportMode: artifact.diagnostics.spatialSupportMode || null,
      spatialSupportAssessedRainEdges: artifact.diagnostics.spatialSupportAssessedRainEdges ?? null,
      spatialSupportFlaggedRainEdges: artifact.diagnostics.spatialSupportFlaggedRainEdges ?? null,
      spatialSupportRejectedRainEdges: artifact.diagnostics.spatialSupportRejectedRainEdges ?? null,
      clusteredFlaggedObserverSeeds: artifact.diagnostics.clusteredFlaggedObserverSeeds ?? null,
      options: artifact.diagnostics.options || null,
    } : null,
  };
}

export async function saveCandidateHistory(artifact, meta = {}) {
  const record = compactHistoryRecord(artifact, meta);
  const raw = JSON.stringify(record);

  if (configuredStore()) {
    const seenKey = HISTORY_SEEN_PREFIX + record.generatedAt;
    const seen = await redis(["SET", seenKey, "1", "NX", "EX", 7 * 24 * 60 * 60]);
    if (!seen) return { stored: false, reason: "duplicate" };
    await redisPipeline([
      ["LPUSH", HISTORY_KEY, raw],
      ["LTRIM", HISTORY_KEY, 0, maxRuns() - 1],
    ]);
    return { stored: true, backend: "redis", key: HISTORY_KEY };
  }

  if (process.env.VERCEL) return { stored: false, reason: "store_not_configured" };

  await appendFile(LOCAL_HISTORY_PATH, raw + "\n", "utf8");
  return { stored: true, backend: "file", path: LOCAL_HISTORY_PATH };
}

export async function loadCandidateHistory(limit = maxRuns()) {
  const n = Math.max(1, Math.min(Number(limit) || maxRuns(), maxRuns()));
  if (configuredStore()) {
    const rows = await redis(["LRANGE", HISTORY_KEY, 0, n - 1]);
    return (Array.isArray(rows) ? rows : [])
      .map(row => {
        try { return JSON.parse(row); } catch { return null; }
      })
      .filter(Boolean);
  }

  if (process.env.VERCEL) return [];

  try {
    const text = await readFile(LOCAL_HISTORY_PATH, "utf8");
    return text.trim().split(/\n+/)
      .slice(-n)
      .reverse()
      .map(row => {
        try { return JSON.parse(row); } catch { return null; }
      })
      .filter(Boolean);
  } catch {
    return [];
  }
}
