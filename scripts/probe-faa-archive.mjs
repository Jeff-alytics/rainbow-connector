import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const FAA_API = "https://weathercams.faa.gov/api";
export const FAA_ARCHIVE_PROBE_VERSION = "faa-retrospective-archive-probe-v1";
export const FAA_CATALOG_FILE = "faa-sites-compact.json";
export const MODEL_VERSIONS = Object.freeze({
  sunlightV1: "production-sunlight-v1",
  sunlightV2: "sunlight-v2-shadow-2026-07-v3",
});

const FAA_HEADERS = Object.freeze({
  Referer: "https://weathercams.faa.gov/",
  Origin: "https://weathercams.faa.gov",
  "User-Agent": "Mozilla/5.0 rainbow-connector-retrospective-probe",
});
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function parseDate(value, label) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error(`${label} must be an ISO date`);
  return date;
}

export function buildImagesUrl(siteId, start, end) {
  const startDate = parseDate(start, "start");
  const endDate = parseDate(end, "end");
  if (endDate <= startDate) throw new Error("end must be after start");
  const query = new URLSearchParams({ startTime: startDate.toISOString(), endTime: endDate.toISOString() });
  return `${FAA_API}/sites/${encodeURIComponent(siteId)}/images?${query}`;
}

function stableSiteKey(site) {
  return `${String(site.state || "").toUpperCase()}|${String(site.id)}`;
}

export function selectProbeSites(catalog, options = {}) {
  const requestedIds = new Set((options.siteIds || []).map(String));
  const requestedStates = new Set((options.states || []).map(state => String(state).toUpperCase()));
  const filtered = (catalog || [])
    .filter(site => !requestedIds.size || requestedIds.has(String(site.id)))
    .filter(site => !requestedStates.size || requestedStates.has(String(site.state || "").toUpperCase()))
    .filter(site => Number.isFinite(Number(site.lat)) && Number.isFinite(Number(site.lon)))
    .sort((a, b) => stableSiteKey(a).localeCompare(stableSiteKey(b)));
  const limit = Number(options.sampleSites);
  return Number.isInteger(limit) && limit > 0 ? filtered.slice(0, limit) : filtered;
}

function imageTime(image) {
  const value = new Date(image?.imageDatetime || image?.capturedAt || image?.timestamp);
  return Number.isFinite(value.getTime()) ? value : null;
}

export function summarizeImages(site, payload, start, end) {
  const images = Array.isArray(payload?.payload) ? payload.payload
    : Array.isArray(payload) ? payload : [];
  const startDate = parseDate(start, "start");
  const endDate = parseDate(end, "end");
  const byCamera = new Map();
  const usable = [];
  for (const image of images) {
    const date = imageTime(image);
    if (!date || date < startDate || date > endDate) continue;
    const cameraId = String(image.cameraId ?? image.camera?.id ?? "unknown");
    const entry = byCamera.get(cameraId) || { cameraId, imageCount: 0, firstImageAt: null, lastImageAt: null, usableFrameCount: 0 };
    entry.imageCount += 1;
    entry.firstImageAt = !entry.firstImageAt || date < new Date(entry.firstImageAt) ? date.toISOString() : entry.firstImageAt;
    entry.lastImageAt = !entry.lastImageAt || date > new Date(entry.lastImageAt) ? date.toISOString() : entry.lastImageAt;
    if (image.imageUri) { entry.usableFrameCount += 1; usable.push(date); }
    byCamera.set(cameraId, entry);
  }
  const times = usable.sort((a, b) => a - b);
  return {
    siteId: site.id,
    name: site.name,
    state: site.state,
    lat: site.lat,
    lon: site.lon,
    imageCount: [...byCamera.values()].reduce((sum, camera) => sum + camera.imageCount, 0),
    cameraCount: byCamera.size,
    usableFrameCount: times.length,
    oldestImageAt: times[0]?.toISOString() || null,
    newestImageAt: times.at(-1)?.toISOString() || null,
    cameras: [...byCamera.values()].sort((a, b) => a.cameraId.localeCompare(b.cameraId)),
  };
}

async function loadCatalog() {
  const raw = await readFile(path.join(ROOT, FAA_CATALOG_FILE), "utf8");
  return { catalog: JSON.parse(raw), sha256: createHash("sha256").update(raw).digest("hex") };
}

async function probeSite(site, start, end) {
  const url = buildImagesUrl(site.id, start, end);
  try {
    const response = await fetch(url, { headers: FAA_HEADERS, signal: AbortSignal.timeout(20_000) });
    if (!response.ok) return { siteId: site.id, name: site.name, state: site.state, status: "http_error", httpStatus: response.status, error: `FAA returned ${response.status}` };
    const payload = await response.json();
    return { ...summarizeImages(site, payload, start, end), status: "ok", httpStatus: response.status };
  } catch (error) {
    return { siteId: site.id, name: site.name, state: site.state, status: "error", httpStatus: null, error: String(error?.message || error) };
  }
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token === "--help") args.help = true;
    else if (token.startsWith("--")) args[token.slice(2)] = argv[++i];
    else throw new Error(`Unexpected argument: ${token}`);
  }
  return args;
}

function usage() {
  return `Read-only FAA archive probe. No images or production records are written.

Usage:
  node scripts/probe-faa-archive.mjs --start ISO --end ISO [options]

Options:
  --sample-sites N       Probe the first N deterministic catalog sites
  --states OH,PA         Restrict to state abbreviations
  --site-ids 123,456     Probe explicit FAA site IDs
  --concurrency N        Concurrent requests (default 4)
  --output PATH          Write the manifest JSON; otherwise print it
`;
}

export async function runProbe(options) {
  const start = parseDate(options.start, "start").toISOString();
  const end = parseDate(options.end, "end").toISOString();
  const { catalog: allSites, sha256: catalogSha256 } = await loadCatalog();
  const sites = selectProbeSites(allSites, {
    sampleSites: options.sampleSites,
    states: options.states,
    siteIds: options.siteIds,
  });
  const concurrency = Math.max(1, Math.min(16, Number(options.concurrency) || 4));
  const results = [];
  for (let offset = 0; offset < sites.length; offset += concurrency) {
    results.push(...await Promise.all(sites.slice(offset, offset + concurrency).map(site => probeSite(site, start, end))));
  }
  const successful = results.filter(result => result.status === "ok");
  const times = successful.flatMap(result => [result.oldestImageAt, result.newestImageAt]).filter(Boolean).map(value => new Date(value));
  return {
    schemaVersion: FAA_ARCHIVE_PROBE_VERSION,
    generatedAt: new Date().toISOString(),
    archiveApi: FAA_API,
    catalog: { file: FAA_CATALOG_FILE, sha256: catalogSha256, siteCount: allSites.length },
    modelVersions: MODEL_VERSIONS,
    requestedWindow: { start, end },
    sitesRequested: sites.length,
    sites: results,
    summary: {
      sites: results.length,
      sitesWithImages: successful.filter(result => result.imageCount > 0).length,
      sitesWithUsableFrames: successful.filter(result => result.usableFrameCount > 0).length,
      totalImages: successful.reduce((sum, result) => sum + result.imageCount, 0),
      totalUsableFrames: successful.reduce((sum, result) => sum + result.usableFrameCount, 0),
      oldestImageAt: times.length ? new Date(Math.min(...times)).toISOString() : null,
      newestImageAt: times.length ? new Date(Math.max(...times)).toISOString() : null,
      errors: results.filter(result => result.status !== "ok").length,
    },
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (options.help) { console.log(usage()); process.exit(0); }
    if (!options.start || !options.end) throw new Error("--start and --end are required");
    options.sampleSites = options["sample-sites"];
    options.states = options.states ? String(options.states).split(",").filter(Boolean) : undefined;
    options.siteIds = options["site-ids"] ? String(options["site-ids"]).split(",").filter(Boolean) : undefined;
    const manifest = await runProbe(options);
    const serialized = `${JSON.stringify(manifest, null, 2)}\n`;
    if (options.output) {
      await mkdir(path.dirname(path.resolve(options.output)), { recursive: true });
      await writeFile(options.output, serialized, "utf8");
      console.error(`Wrote ${options.output}`);
    } else console.log(serialized);
  } catch (error) {
    console.error(error.message || error);
    console.error(usage());
    process.exitCode = 1;
  }
}
