import { list } from "@vercel/blob";
import { configuredStore, redis } from "./alert-common.mjs";

export const SATELLITE_ARTIFACT_KEY = "rainbow:satellite:candidates:latest";
export const CANDIDATE_SCHEMA_VERSION = 3;
const BLOB_PATHNAME = "satellite-candidates.json";

export function satelliteArtifactError(artifact) {
  if (!artifact || typeof artifact !== "object") return "artifact must be an object";
  if (artifact.schemaVersion !== CANDIDATE_SCHEMA_VERSION) {
    return `artifact.schemaVersion must be ${CANDIDATE_SCHEMA_VERSION}`;
  }
  if (!Array.isArray(artifact.candidates)) return "artifact.candidates must be an array";
  const expiresAt = artifact.expiresAt ? new Date(artifact.expiresAt).getTime() : 0;
  if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) return "artifact.expiresAt must be in the future";
  return "";
}

export function markSatelliteArtifact(artifact, storage) {
  return {
    ...artifact,
    dataStatus: artifact.stale ? "stale" : "live",
    satelliteEnriched: true,
    satelliteStorage: storage,
  };
}

async function loadLatestBlobSatelliteArtifact() {
  if (!process.env.BLOB_READ_WRITE_TOKEN) return null;
  const result = await list({
    prefix: BLOB_PATHNAME,
    limit: 10,
    token: process.env.BLOB_READ_WRITE_TOKEN,
  });
  const blobs = (result.blobs || [])
    .filter(blob => blob.pathname === BLOB_PATHNAME)
    .sort((a, b) => new Date(b.uploadedAt || 0) - new Date(a.uploadedAt || 0));
  const latest = blobs[0];
  if (!latest?.url) return null;
  const response = await fetch(latest.url, { cache: "no-store" });
  if (!response.ok) return null;
  return response.json();
}

export async function loadFreshBlobSatelliteArtifact() {
  try {
    const artifact = await loadLatestBlobSatelliteArtifact();
    if (!artifact) return null;
    return satelliteArtifactError(artifact) ? null : markSatelliteArtifact(artifact, "blob");
  } catch {
    return null;
  }
}

export async function loadRecentBlobSatelliteArtifact(maxAgeMinutes = 20) {
  try {
    const artifact = await loadLatestBlobSatelliteArtifact();
    if (artifact?.schemaVersion !== CANDIDATE_SCHEMA_VERSION || !Array.isArray(artifact.candidates)) return null;
    const generatedAt = new Date(artifact.generatedAt || 0).getTime();
    const age = Date.now() - generatedAt;
    if (!Number.isFinite(generatedAt) || age < 0 || age > maxAgeMinutes * 60000) return null;
    return artifact;
  } catch {
    return null;
  }
}

export async function loadFreshStoredSatelliteArtifact() {
  if (!configuredStore()) return null;
  try {
    const raw = await redis(["GET", SATELLITE_ARTIFACT_KEY]);
    if (!raw) return null;
    const artifact = typeof raw === "string" ? JSON.parse(raw) : raw;
    return satelliteArtifactError(artifact) ? null : markSatelliteArtifact(artifact, "redis");
  } catch {
    return null;
  }
}

export async function loadRecentStoredSatelliteArtifact(maxAgeMinutes = 20) {
  if (!configuredStore()) return null;
  try {
    const raw = await redis(["GET", SATELLITE_ARTIFACT_KEY]);
    if (!raw) return null;
    const artifact = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (artifact?.schemaVersion !== CANDIDATE_SCHEMA_VERSION || !Array.isArray(artifact.candidates)) return null;
    const generatedAt = new Date(artifact.generatedAt || 0).getTime();
    const age = Date.now() - generatedAt;
    if (!Number.isFinite(generatedAt) || age < 0 || age > maxAgeMinutes * 60000) return null;
    return artifact;
  } catch {
    return null;
  }
}

export async function saveStoredSatelliteArtifact(artifact) {
  if (!configuredStore()) throw new Error("satellite artifact storage is not configured");
  const error = satelliteArtifactError(artifact);
  if (error) throw new Error(error);
  const expiresAt = new Date(artifact.expiresAt).getTime();
  const ttlSeconds = Math.max(60, Math.ceil((expiresAt - Date.now()) / 1000) + 300);
  await redis(["SET", SATELLITE_ARTIFACT_KEY, JSON.stringify(artifact), "EX", ttlSeconds]);
  return { key: SATELLITE_ARTIFACT_KEY, ttlSeconds };
}
