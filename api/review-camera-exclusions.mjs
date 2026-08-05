import { createHash } from 'node:crypto';
import { redis } from './alert-common.mjs';

export const REVIEW_CAMERA_EXCLUSIONS_KEY = 'rainbow:review:camera-exclusions:v1';
const REGISTRY_SCHEMA = 'review-camera-exclusions.v1';
const ZERO_HASH = '0'.repeat(64);
export const REVIEW_CAMERA_EXCLUSION_DAYS = 30;
const CAS_RETRIES = 8;
const CAS_LUA = `
local current = redis.call('GET', KEYS[1])
if ARGV[1] == '__missing__' then
  if current then return 0 end
elseif not current or redis.sha1hex(current) ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
`;

function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

function sha1(value) {
  return createHash('sha1').update(value).digest('hex');
}

function recordPayload(entry) {
  return {
    schemaVersion: entry.schemaVersion,
    cameraKey: entry.cameraKey,
    source: entry.source ?? null,
    cameraName: entry.cameraName ?? null,
    eventId: entry.eventId ?? null,
    reason: entry.reason,
    markedAt: entry.markedAt,
    reviewableAfter: entry.reviewableAfter,
    previousHash: entry.previousHash,
  };
}

function cleanRegistry(value) {
  if (!value || value.schemaVersion !== REGISTRY_SCHEMA || !Array.isArray(value.entries)) {
    return { schemaVersion: REGISTRY_SCHEMA, entries: [] };
  }
  let previousHash = ZERO_HASH;
  for (const entry of value.entries) {
    if (entry?.previousHash !== previousHash
      || entry?.recordHash !== sha256(JSON.stringify(recordPayload(entry)))) {
      throw new Error('Camera exclusion registry hash chain is invalid.');
    }
    previousHash = entry.recordHash;
  }
  return { schemaVersion: REGISTRY_SCHEMA, entries: value.entries };
}

export function cameraExclusionDays() {
  return REVIEW_CAMERA_EXCLUSION_DAYS;
}

export function buildCameraExclusionEntry(input, previousHash = ZERO_HASH, markedAt = new Date().toISOString()) {
  const cameraKey = String(input?.cameraKey || '').trim();
  if (!cameraKey) throw new Error('A stable camera key is required for exclusion.');
  const reviewableAfter = new Date(Date.parse(markedAt)
    + cameraExclusionDays() * 24 * 60 * 60 * 1000).toISOString();
  const payload = {
    schemaVersion: 'review-camera-exclusion.v1',
    cameraKey,
    source: String(input?.source || '').trim() || null,
    cameraName: String(input?.cameraName || '').trim() || null,
    eventId: String(input?.eventId || '').trim() || null,
    reason: 'reviewer_bad_view',
    markedAt,
    reviewableAfter,
    previousHash,
  };
  return { ...payload, recordHash: sha256(JSON.stringify(recordPayload(payload))) };
}

export function activeCameraExclusionMap(registry, now = Date.now()) {
  const latest = new Map();
  for (const entry of cleanRegistry(registry).entries) {
    if (!entry?.cameraKey || !entry?.markedAt) continue;
    latest.set(entry.cameraKey, entry);
  }
  return new Map([...latest].filter(([, entry]) => Date.parse(entry.reviewableAfter || 0) > now));
}

export async function loadCameraExclusionRegistry() {
  const raw = await redis(['GET', REVIEW_CAMERA_EXCLUSIONS_KEY]);
  return cleanRegistry(raw ? JSON.parse(raw) : null);
}

export async function appendCameraExclusion(input, attempt = 0) {
  const cameraKey = String(input?.cameraKey || '').trim();
  if (!cameraKey) throw new Error('A stable camera key is required for exclusion.');
  const raw = await redis(['GET', REVIEW_CAMERA_EXCLUSIONS_KEY]);
  const registry = cleanRegistry(raw ? JSON.parse(raw) : null);
  const previous = registry.entries.at(-1) || null;
  const eventId = String(input?.eventId || '').trim() || null;
  const duplicate = [...registry.entries].reverse().find(entry =>
    entry.cameraKey === cameraKey && entry.eventId === eventId
      && entry.reason === 'reviewer_bad_view');
  if (duplicate) return { registry, entry: duplicate, duplicate: true };
  const entry = buildCameraExclusionEntry(input, previous?.recordHash || ZERO_HASH);
  const next = { schemaVersion: REGISTRY_SCHEMA, entries: [...registry.entries, entry] };
  const stored = await redis(['EVAL', CAS_LUA, 1, REVIEW_CAMERA_EXCLUSIONS_KEY,
    raw == null ? '__missing__' : sha1(raw), JSON.stringify(next)]);
  if (Number(stored) !== 1) {
    if (attempt + 1 >= CAS_RETRIES) throw new Error('Camera exclusion update did not converge.');
    return appendCameraExclusion(input, attempt + 1);
  }
  return { registry: next, entry, duplicate: false };
}
