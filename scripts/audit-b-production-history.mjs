#!/usr/bin/env node
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { loadCandidateHistory } from "../api/candidate-history-common.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = path.join(ROOT, "validation", "audit-b", "regression-fixture-v1.json");
const outputPath = path.join(ROOT, "validation", "audit-b", "production-history-replay-v1.json");

function distanceKm(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b.lat - a.lat) * rad;
  const dLon = (b.lon - a.lon) * rad;
  const lat1 = a.lat * rad;
  const lat2 = b.lat * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 6371 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
}

function compact(candidate, generatedAt, sourceList, observer) {
  return {
    generatedAt,
    sourceList,
    candidateId: candidate.id || null,
    verdict: candidate.verdict || (sourceList === "candidates" ? "go" : "watch"),
    lat: candidate.lat,
    lon: candidate.lon,
    distanceKm: Math.round(distanceKm(observer, candidate) * 10) / 10,
    score: candidate.evidence?.score ?? null,
    sunElevationDeg: candidate.evidence?.sunElevationDeg ?? null,
    rainDistanceKm: candidate.evidence?.rainPoint?.distanceKm ?? null,
    selectionReason: candidate.evidence?.selectionReason ?? null,
  };
}

function hash(payload) {
  return createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
const runs = await loadCandidateHistory(2016);
const social = fixture.events.filter(event => event.evidenceClass === "social_report" && Number.isFinite(event.observer?.lat));
const events = social.map(event => {
  const start = Date.parse(event.observation.windowStart);
  const end = Date.parse(event.observation.windowEnd);
  const matches = [];
  for (const run of runs) {
    const generated = Date.parse(run.generatedAt);
    if (!Number.isFinite(generated) || generated < start || generated > end) continue;
    for (const [sourceList, candidates] of [["candidates", run.candidates || []], ["possibleCandidates", run.possibleCandidates || []]]) {
      for (const candidate of candidates) {
        if (!Number.isFinite(candidate.lat) || !Number.isFinite(candidate.lon)) continue;
        const row = compact(candidate, run.generatedAt, sourceList, event.observer);
        if (row.distanceKm <= 100) matches.push(row);
      }
    }
  }
  matches.sort((a, b) => a.distanceKm - b.distanceKm || Date.parse(a.generatedAt) - Date.parse(b.generatedAt));
  const nearest = matches[0] || null;
  return {
    eventId: event.id,
    windowStart: event.observation.windowStart,
    windowEnd: event.observation.windowEnd,
    scansInWindow: runs.filter(run => Date.parse(run.generatedAt) >= start && Date.parse(run.generatedAt) <= end).length,
    candidateWithin15Km: matches.some(match => match.distanceKm <= 15),
    candidateWithin40Km: matches.some(match => match.distanceKm <= 40),
    nearestCandidate: nearest,
    matchesWithin40Km: matches.filter(match => match.distanceKm <= 40),
  };
});

const payload = {
  schemaVersion: 1,
  fixtureId: fixture.fixtureId,
  fixtureContentSha256: fixture.contentSha256,
  historyFirstRunAt: runs.at(-1)?.generatedAt || null,
  historyLastRunAt: runs[0]?.generatedAt || null,
  events,
};
payload.contentSha256 = hash(payload);
await writeFile(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ output: outputPath, historyRuns: runs.length, events }, null, 2));
