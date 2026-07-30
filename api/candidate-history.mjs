import {
  baseUrl,
  distKm,
  json,
  lookupZip,
  verifySecret,
} from "./alert-common.mjs";
import { loadCandidateHistory } from "./candidate-history-common.mjs";

export const config = {
  maxDuration: 10,
};

function numberParam(value, fallback, min, max) {
  const n = Number(Array.isArray(value) ? value[0] : value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function stringParam(value) {
  return String(Array.isArray(value) ? value[0] : value || "").trim();
}

function ageCutoff(hours) {
  return Date.now() - hours * 60 * 60 * 1000;
}

function candidateDistance(target, candidate) {
  return distKm(target.lat, target.lon, candidate.lat, candidate.lon);
}

function filterRunsByAge(runs, hours) {
  const cutoff = ageCutoff(hours);
  return runs.filter(run => {
    const t = run.generatedAt ? new Date(run.generatedAt).getTime() : 0;
    return t && t >= cutoff;
  });
}

function summarizeRuns(runs) {
  const times = runs
    .map(run => run.generatedAt ? new Date(run.generatedAt).getTime() : 0)
    .filter(Boolean)
    .sort((a, b) => a - b);
  return {
    runs: runs.length,
    earliestGeneratedAt: times.length ? new Date(times[0]).toISOString() : null,
    latestGeneratedAt: times.length ? new Date(times[times.length - 1]).toISOString() : null,
  };
}

export default async function handler(req, res) {
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    json(res, 405, { ok: false, error: "Method not allowed." });
    return;
  }

  const hours = numberParam(req.query.hours, 24, 1, 168);
  const radiusKm = numberParam(req.query.radiusKm, 55, 1, 250);
  const limit = numberParam(req.query.limit, 288, 1, 2016);
  const zip = stringParam(req.query.zip).match(/\d{5}/)?.[0] || "";
  const lat = Number(req.query.lat);
  const lon = Number(req.query.lon);
  const wantsAll = stringParam(req.query.all) === "1";

  let target = null;
  if (zip) {
    target = await lookupZip(zip);
    if (!target) {
      json(res, 400, { ok: false, error: "Unknown ZIP." });
      return;
    }
  } else if (Number.isFinite(lat) && Number.isFinite(lon)) {
    target = { lat, lon, place: `${lat.toFixed(4)}, ${lon.toFixed(4)}` };
  }

  if (wantsAll && !verifySecret(req)) {
    json(res, 401, { ok: false, error: "Unauthorized." });
    return;
  }

  const runs = filterRunsByAge(await loadCandidateHistory(limit), hours);
  const summary = summarizeRuns(runs);

  if (!target) {
    json(res, 200, {
      ok: true,
      storage: process.env.VERCEL ? "configured production store or empty" : "configured store or local file",
      hours,
      ...summary,
      candidates: wantsAll
        ? runs.map(run => ({
            generatedAt: run.generatedAt,
            expiresAt: run.expiresAt,
            count: run.candidates?.length || 0,
            candidates: run.candidates || [],
          }))
        : undefined,
      hint: `Add ?zip=48307 or ?lat=42.658&lon=-83.15 to check a location. ${baseUrl(req)}/api/candidate-history?zip=48307`,
    });
    return;
  }

  const matches = [];
  for (const run of runs) {
    for (const c of run.candidates || []) {
      if (!Number.isFinite(c.lat) || !Number.isFinite(c.lon)) continue;
      const d = candidateDistance(target, c);
      if (d <= radiusKm) {
        matches.push({
          generatedAt: run.generatedAt,
          expiresAt: run.expiresAt,
          distanceKm: Number(d.toFixed(1)),
          candidate: c,
        });
      }
    }
  }

  matches.sort((a, b) => new Date(b.generatedAt).getTime() - new Date(a.generatedAt).getTime() || a.distanceKm - b.distanceKm);

  json(res, 200, {
    ok: true,
    target,
    radiusKm,
    hours,
    ...summary,
    matchCount: matches.length,
    matches,
  });
}
