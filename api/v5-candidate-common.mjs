import { configuredStore, redis } from "./alert-common.mjs";

export const V5_DAY_PREFIX = "rainbow:review:v5:day:";
const RETENTION_SECONDS = 14 * 24 * 60 * 60;
const UPSERT_DAY_LUA = `
local raw = redis.call('GET', KEYS[1])
local incoming = cjson.decode(ARGV[1])
local current = raw and cjson.decode(raw) or {schemaVersion='v5-candidate-day.v1', date=incoming.date, items={}, scanHashes={}, sourceScanHashes={}}
current.scanHashes = current.scanHashes or {}
current.sourceScanHashes = current.sourceScanHashes or {}
if current.scanHashes[incoming.scanHash] then return -((#(current.items or {})) + 1) end
local byFamily = {}
for _, item in ipairs(current.items or {}) do byFamily[item.familyEventId] = item end
for _, item in ipairs(incoming.items or {}) do
  local old = byFamily[item.familyEventId]
  if old then
    local scans = (tonumber(old.scanSelections) or 0) + (tonumber(item.scanSelections) or 0)
    local geometries = (tonumber(old.geometrySelections) or 0) + (tonumber(item.geometrySelections) or 0)
    local geometryById = {}
    for _, geometry in ipairs(old.geometries or {}) do geometryById[geometry.geometryId or geometry.candidateId] = geometry end
    for _, geometry in ipairs(item.geometries or {}) do
      local key = geometry.geometryId or geometry.candidateId
      local prior = geometryById[key]
      if not prior or (tonumber(geometry.score) or -1) > (tonumber(prior.score) or -1) then geometryById[key] = geometry end
    end
    local mergedGeometries = {}
    for _, geometry in pairs(geometryById) do table.insert(mergedGeometries, geometry) end
    local firstAt = old.firstDetectedAt
    if not firstAt or (item.firstDetectedAt and item.firstDetectedAt < firstAt) then firstAt = item.firstDetectedAt end
    local lastAt = old.lastDetectedAt
    if not lastAt or (item.lastDetectedAt and item.lastDetectedAt > lastAt) then lastAt = item.lastDetectedAt end
    if (tonumber(item.score) or -1) > (tonumber(old.score) or -1) then
      old = item
      byFamily[item.familyEventId] = old
    end
    old.scanSelections = scans
    old.geometrySelections = geometries
    old.geometries = mergedGeometries
    old.firstDetectedAt = firstAt
    old.lastDetectedAt = lastAt
  else
    byFamily[item.familyEventId] = item
  end
end
current.items = {}
for _, item in pairs(byFamily) do table.insert(current.items, item) end
current.updatedAt = incoming.updatedAt
if not current.sourceScanHashes[incoming.sourceScanHash] then
  current.scanCount = (tonumber(current.scanCount) or 0) + 1
  current.sourceScanHashes[incoming.sourceScanHash] = true
end
current.scanHashes[incoming.scanHash] = true
redis.call('SET', KEYS[1], cjson.encode(current), 'EX', ARGV[2])
return #current.items
`;

function timeMs(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isFinite(parsed) ? parsed : null;
}

function finite(value) {
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

export function validV5CandidateScan(feed) {
  const observed = timeMs(feed?.scanTime);
  if (feed?.schemaVersion !== "v5-candidate-scan.v1" || !observed || !Array.isArray(feed?.items)) return false;
  if (feed.acquisitionEnabled !== false) return false;
  return feed.items.every(item => item?.predictionId && item?.familyEventId
    && Number.isFinite(Number(item?.lat)) && Number.isFinite(Number(item?.lon))
    && Array.isArray(item?.cohortMembership) && Array.isArray(item?.geometries)
    && item.geometries.length > 0 && item.geometries.every(geometry => geometry?.geometryId
      && Number.isFinite(Number(geometry?.lat)) && Number.isFinite(Number(geometry?.lon))));
}

function laneOrder(value) {
  return ({ observed_core: 0, observed_extended: 1, physical_audit: 2, unresolved: 3 })[value] ?? 3;
}

function sortedItems(items) {
  return [...items].sort((a, b) => laneOrder(a.solarLane) - laneOrder(b.solarLane)
    || (finite(a.rankWithinScan) ?? 1e9) - (finite(b.rankWithinScan) ?? 1e9)
    || String(a.familyEventId).localeCompare(String(b.familyEventId)));
}

function mergeGeometries(left, right) {
  const byId = new Map();
  for (const geometry of [...(left || []), ...(right || [])]) {
    const prior = byId.get(geometry.geometryId);
    if (!prior || (finite(geometry.score) ?? -1) > (finite(prior.score) ?? -1)) {
      byId.set(geometry.geometryId, geometry);
    }
  }
  return [...byId.values()].sort((a, b) => (finite(a.rankWithinScan) ?? 1e9) - (finite(b.rankWithinScan) ?? 1e9)
    || String(a.geometryId).localeCompare(String(b.geometryId)));
}

export function aggregateV5CandidateScans(scans) {
  const days = new Map();
  for (const scan of scans || []) {
    if (!validV5CandidateScan(scan)) continue;
    const day = String(scan.scanTime).slice(0, 10);
    if (!days.has(day)) days.set(day, new Map());
    const families = days.get(day);
    const withinScan = new Map();
    for (const item of scan.items) {
      const familyId = item.familyEventId || item.predictionId;
      const current = withinScan.get(familyId);
      if (!current) {
        withinScan.set(familyId, { ...item, geometrySelections: Math.max(1, Number(item.geometrySelections) || 1) });
      } else {
        current.geometrySelections += Math.max(1, Number(item.geometrySelections) || 1);
        const geometries = mergeGeometries(current.geometries, item.geometries);
        if ((finite(item.score) ?? -1) > (finite(current.score) ?? -1)) {
          withinScan.set(familyId, { ...item, geometrySelections: current.geometrySelections, geometries });
        } else current.geometries = geometries;
      }
    }
    for (const [familyId, item] of withinScan) {
      const existing = families.get(familyId);
      if (!existing) {
        families.set(familyId, { ...item, scanSelections: 1,
          firstDetectedAt: item.detectedAt, lastDetectedAt: item.detectedAt });
        continue;
      }
      const counts = {
        scanSelections: existing.scanSelections + 1,
        geometrySelections: existing.geometrySelections + item.geometrySelections,
        geometries: mergeGeometries(existing.geometries, item.geometries),
        firstDetectedAt: timeMs(item.detectedAt) < timeMs(existing.firstDetectedAt)
          ? item.detectedAt : existing.firstDetectedAt,
        lastDetectedAt: timeMs(item.detectedAt) > timeMs(existing.lastDetectedAt)
          ? item.detectedAt : existing.lastDetectedAt,
      };
      if ((finite(item.score) ?? -1) > (finite(existing.score) ?? -1)) {
        families.set(familyId, { ...item, ...counts });
      } else Object.assign(existing, counts);
    }
  }
  return [...days.entries()]
    .sort(([left], [right]) => right.localeCompare(left))
    .map(([date, families]) => ({ date, items: sortedItems(families.values()) }));
}

export async function storeV5CandidateScan(feed) {
  if (!configuredStore()) return { stored: false, reason: "store_not_configured", items: feed?.items?.length || 0 };
  if (!validV5CandidateScan(feed)) throw new Error("Invalid V5 candidate scan.");
  const [day] = aggregateV5CandidateScans([feed]);
  const payload = { ...day, schemaVersion: "v5-candidate-day.v1", updatedAt: new Date().toISOString(),
    scanHash: feed.predictionManifestSha256 || String(feed.scanTime),
    sourceScanHash: feed.sourcePredictionManifestSha256 || feed.predictionManifestSha256 || String(feed.scanTime) };
  const families = await redis(["EVAL", UPSERT_DAY_LUA, 1, V5_DAY_PREFIX + day.date,
    JSON.stringify(payload), RETENTION_SECONDS]);
  const duplicate = Number(families) < 0;
  return { stored: !duplicate, duplicate,
    day: day.date, items: feed.items.length,
    families: duplicate ? Math.max(0, Math.abs(Number(families)) - 1) : Number(families) || 0,
    databaseCommands: 1 };
}

function utcDay(offset) {
  const date = new Date(Date.now() - offset * 24 * 60 * 60 * 1000);
  return date.toISOString().slice(0, 10);
}

export async function loadV5CandidateDays(options = {}) {
  if (!configuredStore()) return [];
  const count = Math.max(1, Math.min(Number(options.days) || 7, 14));
  const keys = Array.from({ length: count }, (_, index) => V5_DAY_PREFIX + utcDay(index));
  const values = await redis(["MGET", ...keys]);
  return (values || []).flatMap(raw => {
    if (!raw) return [];
    try {
      const day = JSON.parse(raw);
      const { scanHashes: _scanHashes, sourceScanHashes: _sourceScanHashes, ...safeDay } = day;
      return [{ ...safeDay, items: sortedItems(day.items || []) }];
    } catch {
      return [];
    }
  }).sort((a, b) => String(b.date).localeCompare(String(a.date)));
}
