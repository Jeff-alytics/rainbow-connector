import { configuredStore } from "./alert-common.mjs";
import {
  CANDIDATE_SCHEMA_VERSION,
  loadFreshBlobSatelliteArtifact,
  loadFreshStoredSatelliteArtifact,
} from "./satellite-artifact-common.mjs";

export const config = {
  maxDuration: 15,
};

const MAX_RADAR_AGE_MIN = 20;
const MAX_SATELLITE_AGE_MIN = 12;

async function fetchJson(url, timeoutMs = 6000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

function ageMinutes(value) {
  const time = value ? new Date(value).getTime() : NaN;
  return Number.isFinite(time) ? Math.max(0, (Date.now() - time) / 60000) : null;
}

async function checkRadar() {
  const data = await fetchJson("https://api.rainviewer.com/public/weather-maps.json");
  const latest = data.radar?.past?.at(-1);
  if (!latest?.time) throw new Error("No radar frame");
  const observedAt = new Date(latest.time * 1000).toISOString();
  const age = ageMinutes(observedAt);
  return {
    ok: age != null && age <= MAX_RADAR_AGE_MIN,
    observedAt,
    ageMinutes: age == null ? null : Number(age.toFixed(1)),
    maxAgeMinutes: MAX_RADAR_AGE_MIN,
  };
}

async function checkWeather() {
  const data = await fetchJson(
    "https://api.open-meteo.com/v1/forecast?latitude=39.5&longitude=-98.35&current=cloud_cover,direct_normal_irradiance_instant&forecast_days=1&timezone=GMT",
  );
  if (!data.current?.time) throw new Error("No weather timestamp");
  return {
    ok: true,
    observedAt: data.current.time,
    variables: {
      cloudCoverPct: data.current.cloud_cover ?? null,
      directNormalIrradianceWm2: data.current.direct_normal_irradiance_instant ?? null,
    },
  };
}

async function checkSatellite() {
  const artifact = await loadFreshStoredSatelliteArtifact()
    || await loadFreshBlobSatelliteArtifact();
  if (!artifact) {
    return {
      ok: false,
      source: null,
      reason: "No fresh satellite artifact",
    };
  }
  const age = ageMinutes(artifact.generatedAt);
  return {
    ok: age != null && age <= MAX_SATELLITE_AGE_MIN,
    source: artifact.satelliteStorage || null,
    generatedAt: artifact.generatedAt || null,
    expiresAt: artifact.expiresAt || null,
    ageMinutes: age == null ? null : Number(age.toFixed(1)),
    maxAgeMinutes: MAX_SATELLITE_AGE_MIN,
    candidates: Array.isArray(artifact.candidates) ? artifact.candidates.length : null,
  };
}

function settled(result) {
  if (result.status === "fulfilled") return result.value;
  return {
    ok: false,
    error: result.reason?.message || String(result.reason || "Unknown error"),
  };
}

export default async function handler(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.setHeader("Allow", "GET, HEAD");
    res.status(405).end();
    return;
  }

  const started = Date.now();
  const [radarResult, weatherResult, satelliteResult] = await Promise.allSettled([
    checkRadar(),
    checkWeather(),
    checkSatellite(),
  ]);
  const checks = {
    radar: settled(radarResult),
    weather: settled(weatherResult),
    satellite: settled(satelliteResult),
  };
  const coreHealthy = checks.radar.ok && checks.weather.ok;
  const status = !coreHealthy ? "unhealthy" : checks.satellite.ok ? "healthy" : "degraded";
  const body = {
    ok: status !== "unhealthy",
    status,
    schemaVersion: CANDIDATE_SCHEMA_VERSION,
    checkedAt: new Date().toISOString(),
    runtimeMs: Date.now() - started,
    expectedCadenceMinutes: 5,
    storage: configuredStore()
      ? "redis"
      : process.env.BLOB_READ_WRITE_TOKEN
        ? "blob"
        : "none",
    checks,
  };

  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Rainbow-Health", status);
  if (req.method === "HEAD") res.status(status === "unhealthy" ? 503 : 200).end();
  else res.status(status === "unhealthy" ? 503 : 200).send(JSON.stringify(body));
}
