import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CANDIDATE_SCHEMA_VERSION, loadFreshBlobSatelliteArtifact, loadFreshStoredSatelliteArtifact, markSatelliteArtifact, satelliteArtifactError } from "./satellite-artifact-common.mjs";

export const config = {
  maxDuration: 60,
};

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const SATELLITE_CANDIDATES_PATH = path.join(ROOT, "satellite-candidates.json");
const SATELLITE_CANDIDATES_URL = (process.env.SATELLITE_CANDIDATES_URL || "")
  .trim()
  .replace(/\\r|\\n|\r|\n/g, "");

function setCandidateHeaders(res, stale = false, source = null, cdnMaxAge = 300) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "public, max-age=0");
  res.setHeader("Vercel-CDN-Cache-Control", stale
    ? "max-age=60, stale-while-revalidate=300"
    : `max-age=${cdnMaxAge}, stale-while-revalidate=60`);
  res.setHeader("X-Rainbow-Candidate-Source", source || (stale ? "fallback" : "detector"));
}

async function loadFreshLocalSatelliteArtifact() {
  try {
    const artifact = JSON.parse(await readFile(SATELLITE_CANDIDATES_PATH, "utf8"));
    if (satelliteArtifactError(artifact)) return null;
    return markSatelliteArtifact(artifact, "file");
  } catch {
    return null;
  }
}

async function loadFreshRemoteSatelliteArtifact() {
  if (!SATELLITE_CANDIDATES_URL) return null;
  try {
    const response = await fetch(SATELLITE_CANDIDATES_URL, { cache: "no-store" });
    if (!response.ok) return null;
    const artifact = await response.json();
    if (satelliteArtifactError(artifact)) return null;
    return markSatelliteArtifact(artifact, "url");
  } catch {
    return null;
  }
}

function publicArtifact(artifact) {
  const { persistenceCandidates, ...visible } = artifact;
  return visible;
}

export default async function handler(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.setHeader("Allow", "GET, HEAD");
    res.status(405).end();
    return;
  }

  const started = Date.now();
  try {
    const satelliteArtifact = await loadFreshStoredSatelliteArtifact()
        || await loadFreshBlobSatelliteArtifact()
        || await loadFreshRemoteSatelliteArtifact()
        || await loadFreshLocalSatelliteArtifact();
    if (satelliteArtifact) {
      satelliteArtifact.runtimeMs = Date.now() - started;
      const visibleArtifact = publicArtifact(satelliteArtifact);
      const source = satelliteArtifact.satelliteStorage === "redis"
        ? "satellite-kv"
        : satelliteArtifact.satelliteStorage === "url"
          ? "satellite-url"
          : satelliteArtifact.satelliteStorage === "blob"
            ? "satellite-blob"
          : "satellite";
      setCandidateHeaders(res, false, source, 60);
      if (req.method === "HEAD") res.status(200).end();
      else res.status(200).send(JSON.stringify(visibleArtifact));
      return;
    }

    throw new Error("No fresh published candidate artifact is available");
  } catch (err) {
    try {
      const fallback = JSON.parse(await readFile(path.join(ROOT, "candidates.json"), "utf8"));
      fallback.stale = true;
      fallback.schemaVersion = CANDIDATE_SCHEMA_VERSION;
      fallback.dataStatus = "stale";
      fallback.error = err?.message || "candidate refresh failed";
      fallback.runtimeMs = Date.now() - started;
      setCandidateHeaders(res, true);
      if (req.method === "HEAD") res.status(200).end();
      else res.status(200).send(JSON.stringify(fallback));
    } catch {
      setCandidateHeaders(res, true);
      res.status(502).send(JSON.stringify({
        generatedAt: new Date().toISOString(),
        candidates: [],
        schemaVersion: CANDIDATE_SCHEMA_VERSION,
        dataStatus: "unavailable",
        stale: true,
        error: err?.message || "candidate refresh failed",
      }));
    }
  }
}
