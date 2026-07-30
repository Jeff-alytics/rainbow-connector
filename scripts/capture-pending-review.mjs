import { capturePendingAlertCaEvidence } from "../api/alertca-common.mjs";
import { capturePendingFaaEvidence } from "../api/go-evidence-common.mjs";
import { capturePendingNimsEvidence } from "../api/usgs-nims-common.mjs";
import { capturePendingWebcoosEvidence } from "../api/webcoos-common.mjs";

const rounds = Math.max(1, Math.min(Number(process.env.REVIEW_CAPTURE_ROUNDS || 4), 10));
const results = [];

for (let round = 1; round <= rounds; round++) {
  results.push({
    round,
    faa: await capturePendingFaaEvidence(4),
    nims: await capturePendingNimsEvidence(3),
    webcoos: await capturePendingWebcoosEvidence(2),
    alertCalifornia: await capturePendingAlertCaEvidence(2),
  });
}

console.log(JSON.stringify({ rounds, results }));
