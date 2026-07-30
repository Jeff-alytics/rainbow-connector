import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { compactNegativeEvent } from "./negative-fixture-common.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const input = process.argv[2];
const output = process.argv[3] || path.join(ROOT, "validation", "audit-b", "negative-precision-fixture-v1.json");
if (!input) throw new Error("Usage: node scripts/build-negative-precision-fixture.mjs <private-events.json> [output.json]");

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

const source = JSON.parse(await readFile(input, "utf8"));
const records = (source.items || [])
  .filter(event => event?.review?.label === "no_rainbow")
  .map(compactNegativeEvent)
  .sort((a, b) => String(a.eventId).localeCompare(String(b.eventId)));
const reviewedTimes = records.map(record => record.reviewedAt).filter(Boolean).sort();
const payload = {
  schemaVersion: 1,
  fixtureId: "rainbow-negative-precision-v1",
  frozenThrough: reviewedTimes.at(-1) || null,
  purpose: "Human-reviewed no-rainbow comparison fixture. Weak evidence is retained transparently but receives little or zero calibration weight.",
  methodology: {
    version: "negative-evidence-v1",
    calibrationMinimumWeight: 0.5,
    unknownBearingWeight: 0,
    requiredForCalibration: ["known bearing within 30 degrees", "camera within 35 km", "at least two distinct frames", "nearest frame within 8 minutes", "adequate reviewed sky view"],
    limitation: "This v1 weight uses camera-bearing difference as a proxy until Phase 1 supplies visible-bow fraction and rain-backed visible-bow fraction.",
  },
  summary: {
    records: records.length,
    calibrationEligible: records.filter(record => record.negativeEvidence.calibrationEligible).length,
    zeroWeight: records.filter(record => record.negativeEvidence.weight === 0).length,
    weakNonzero: records.filter(record => record.negativeEvidence.weight > 0 && !record.negativeEvidence.calibrationEligible).length,
    totalCalibrationWeight: Math.round(records.reduce((sum, record) => sum + record.negativeEvidence.weight, 0) * 1000) / 1000,
  },
  records,
};
payload.contentSha256 = createHash("sha256").update(canonical(payload)).digest("hex");
await writeFile(output, JSON.stringify(payload, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ output, ...payload.summary, sha256: payload.contentSha256 }, null, 2));
