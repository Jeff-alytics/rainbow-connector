import test from "node:test";
import assert from "node:assert/strict";

import { compactHistoryRecord } from "../api/candidate-history-common.mjs";

test("candidate history retains spatial-support shadow diagnostics", () => {
  const record = compactHistoryRecord({
    generatedAt: "2026-08-03T00:00:00Z",
    diagnostics: {
      spatialSupportMethodVersion: "mrms-adjacent-wet-cell-v1",
      spatialSupportMode: "shadow",
      spatialSupportAssessedRainEdges: 12,
      spatialSupportFlaggedRainEdges: 2,
      spatialSupportRejectedRainEdges: 0,
      clusteredFlaggedObserverSeeds: 1,
    },
  });
  assert.deepEqual({
    method: record.diagnostics.spatialSupportMethodVersion,
    mode: record.diagnostics.spatialSupportMode,
    assessed: record.diagnostics.spatialSupportAssessedRainEdges,
    flagged: record.diagnostics.spatialSupportFlaggedRainEdges,
    rejected: record.diagnostics.spatialSupportRejectedRainEdges,
    clustered: record.diagnostics.clusteredFlaggedObserverSeeds,
  }, {
    method: "mrms-adjacent-wet-cell-v1",
    mode: "shadow",
    assessed: 12,
    flagged: 2,
    rejected: 0,
    clustered: 1,
  });
});
