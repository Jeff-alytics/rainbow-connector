import { loadCandidateHistory } from "../api/candidate-history-common.mjs";
import { saveGoEvents } from "../api/go-event-common.mjs";

const since = process.env.BACKFILL_SINCE_UTC
  ? new Date(process.env.BACKFILL_SINCE_UTC).getTime()
  : null;
const runs = (await loadCandidateHistory(2016))
  .filter(run => !Number.isFinite(since) || new Date(run.generatedAt).getTime() >= since)
  .slice()
  .sort((a, b) => new Date(a.generatedAt) - new Date(b.generatedAt));

let goScans = 0;
let goCandidates = 0;
let possibleCandidates = 0;
let newEvents = 0;
for (const run of runs) {
  const candidates = (run.candidates || []).map(candidate => ({
    ...candidate,
    verdict: "go",
    persistence: {
      ...(candidate.persistence || {}),
      confirmed: true,
    },
  }));
  const possible = (run.possibleCandidates || []).map(candidate => ({
    ...candidate,
    verdict: "watch",
  }));
  if (candidates.length) {
    goScans++;
    goCandidates += candidates.length;
  }
  possibleCandidates += possible.length;
  const result = await saveGoEvents(
    { ...run, candidates, possibleCandidates: possible },
    { scanScope: "review-backfill-v1" },
  );
  newEvents += Number(result.newEvents || 0);
}

console.log(JSON.stringify({ runs: runs.length, goScans, goCandidates, possibleCandidates, newEvents }));
