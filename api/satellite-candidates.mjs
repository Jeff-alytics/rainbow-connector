import { timingSafeEqual } from "node:crypto";
import { put } from "@vercel/blob";
import { configuredStore, json, readJsonBody } from "./alert-common.mjs";
import { applyCandidatePersistence, evaluateRequiredSources } from "./artifact-policy.mjs";
import { loadRecentBlobSatelliteArtifact, loadRecentStoredSatelliteArtifact, saveStoredSatelliteArtifact, satelliteArtifactError } from "./satellite-artifact-common.mjs";

export const config = {
  maxDuration: 10,
};

export function verifyPublishSecret(req, body = {}) {
  // Artifact publishing and subscriber notifications are separate trust paths.
  // Never let the notification secret shadow the worker's existing publisher credential.
  const primary = String(process.env.SATELLITE_PUBLISH_SECRET || process.env.BLOB_READ_WRITE_TOKEN || "").trim();
  const expected = [primary, String(process.env.SATELLITE_PUBLISH_SECRET_NEXT || "").trim()].filter(Boolean);
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

async function saveBlobSatelliteArtifact(artifact) {
  if (!process.env.BLOB_READ_WRITE_TOKEN) throw new Error("BLOB_READ_WRITE_TOKEN is not configured");
  const blob = await put("satellite-candidates.json", JSON.stringify(artifact), {
    access: "public",
    allowOverwrite: true,
    contentType: "application/json",
    cacheControlMaxAge: 0,
    token: process.env.BLOB_READ_WRITE_TOKEN,
  });
  return { storage: "blob", url: blob.url, pathname: blob.pathname };
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    json(res, 405, { ok: false, error: "Method not allowed." });
    return;
  }

  const body = await readJsonBody(req);
  if (!verifyPublishSecret(req, body)) {
    json(res, 401, { ok: false, error: "Unauthorized." });
    return;
  }
  let artifact = body.artifact && typeof body.artifact === "object" ? body.artifact : body;
  if (artifact.sourceHealth) {
    const health = evaluateRequiredSources(artifact.sourceHealth);
    artifact = { ...artifact, sourceHealthSummary: health };
    if (!health.healthy) {
      json(res, 422, {
        ok: false,
        error: `Required sources are stale or missing: ${health.blocking.join(", ")}`,
        sourceHealth: health,
      });
      return;
    }
  }
  const previous = await loadRecentStoredSatelliteArtifact()
    || await loadRecentBlobSatelliteArtifact();
  artifact = applyCandidatePersistence(artifact, previous);
  const validationError = satelliteArtifactError(artifact);
  if (validationError) {
    json(res, 400, { ok: false, error: validationError });
    return;
  }

  try {
    const stored = configuredStore()
      ? await saveStoredSatelliteArtifact(artifact)
      : await saveBlobSatelliteArtifact(artifact);
    json(res, 200, {
      ok: true,
      generatedAt: artifact.generatedAt || null,
      expiresAt: artifact.expiresAt || null,
      candidates: artifact.candidates.length,
      satelliteDiagnostics: artifact.satelliteDiagnostics || null,
      storage: stored,
    });
  } catch (err) {
    json(res, 502, { ok: false, error: err?.message || "Satellite artifact publish failed." });
  }
}
