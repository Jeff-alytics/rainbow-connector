import { readFile } from "node:fs/promises";
import { loadCandidateHistory } from "../api/candidate-history-common.mjs";
import { matchFaaCamera } from "../api/go-evidence-common.mjs";
import { captureFaaEvidence } from "../api/go-evidence-common.mjs";
import { loadGoEvents, saveGoEvents } from "../api/go-event-common.mjs";

const since = new Date(process.env.AUDIT_SINCE_UTC || Date.now() - 12 * 60 * 60 * 1000).getTime();
const catalog = JSON.parse(await readFile(new URL("../faa-sites-compact.json", import.meta.url), "utf8"));
const runs = (await loadCandidateHistory(2016))
  .filter(run => new Date(run.generatedAt).getTime() >= since)
  .sort((a, b) => new Date(a.generatedAt) - new Date(b.generatedAt));
const matches = [];

for (const run of runs) {
  for (const candidate of run.possibleCandidates || []) {
    const score = Number(candidate?.evidence?.score);
    if (!Number.isFinite(score) || score >= 50) continue;
    const event = {
      firstSeenAt: run.generatedAt,
      representative: {
        ...candidate,
        detectedAt: run.generatedAt,
      },
    };
    const match = matchFaaCamera(event, catalog);
    if (match) matches.push({ event, match, score });
  }
}

const clustered = [];
for (const item of matches.sort((a, b) => new Date(a.event.firstSeenAt) - new Date(b.event.firstSeenAt))) {
  const previous = clustered.find(group =>
    group.match.camera.id === item.match.camera.id
    && Math.abs(new Date(group.event.firstSeenAt) - new Date(item.event.firstSeenAt)) <= 30 * 60 * 1000);
  if (!previous) clustered.push(item);
  else if (item.score > previous.score) Object.assign(previous, item);
}

const headers = {
  Referer: "https://weathercams.faa.gov/",
  Origin: "https://weathercams.faa.gov",
  "User-Agent": "Mozilla/5.0 rainbow-connector-review-audit",
};
const reviewable = [];
for (const item of clustered.sort((a, b) => b.score - a.score)) {
  const center = new Date(item.event.firstSeenAt).getTime();
  const query = new URLSearchParams({
    startTime: new Date(center - 25 * 60 * 1000).toISOString(),
    endTime: new Date(Math.min(Date.now(), center + 25 * 60 * 1000)).toISOString(),
  });
  try {
    const response = await fetch(`https://weathercams.faa.gov/api/sites/${item.match.site.id}/images?${query}`, { headers });
    if (!response.ok) continue;
    const payload = await response.json();
    const frames = (payload.payload || []).filter(frame =>
      String(frame.cameraId) === String(item.match.camera.id) && frame.imageUri);
    if (frames.length >= 3) reviewable.push({
      sourceEvent: item.event,
      cameraId: item.match.camera.id,
      detectedAt: item.event.firstSeenAt,
      score: item.score,
      candidate: {
        lat: item.event.representative.lat,
        lon: item.event.representative.lon,
      },
      camera: {
        site: item.match.site.name,
        state: item.match.site.state,
        direction: item.match.camera.direction,
        distanceKm: Number(item.match.distanceKm.toFixed(1)),
        directionErrorDeg: Number(item.match.bearingDifference.toFixed(1)),
      },
      frames: frames.length,
    });
  } catch {}
}

const seeded = [];
if (process.env.AUDIT_SEED_REVIEW === "1") {
  for (const item of reviewable) {
    const candidate = {
      ...item.sourceEvent.representative,
      verdict: "watch",
    };
    const stored = await saveGoEvents({
      generatedAt: item.detectedAt,
      candidates: [],
      possibleCandidates: [candidate],
    }, {
      includeAllPossibles: true,
      scanScope: `weak-faa-review-v1-${item.cameraId}`,
    });
    const events = await loadGoEvents(1000);
    for (const id of stored.eventIds || []) {
      const event = events.find(value => value.id === id);
      if (!event) continue;
      const capture = await captureFaaEvidence(event);
      seeded.push({ id, score: item.score, capture });
    }
  }
}

console.log(JSON.stringify({
  runs: runs.length,
  belowThresholdCandidates: runs.reduce((total, run) =>
    total + (run.possibleCandidates || []).filter(candidate => Number(candidate?.evidence?.score) < 50).length, 0),
  faaCameraMatches: matches.length,
  clusteredFaaWindows: clustered.length,
  reviewableWindows: reviewable.length,
  items: reviewable.map(({ sourceEvent, cameraId, ...item }) => item),
  seeded,
}));
