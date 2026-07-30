import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

function functionSource(html, name, nextName) {
  const start = html.indexOf(`function ${name}`);
  const end = html.indexOf(`function ${nextName}`, start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  return html.slice(start, end);
}

test("nationwide map hides possible candidates by default and exposes a toggle", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /id="candidate-toggle"[^>]+aria-pressed="false"/);
  assert.match(html, /let showPossibleCandidates = false;/);
  assert.match(html, /showPossibleCandidates \? mapPossibleCandidates\(artifact\) : \[\]/);
});

test("ZIP cards show new strict GO immediately and tightly screen possible candidates within 15 km", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /const ZIP_GO_RADIUS_KM = 15;/);
  assert.match(html, /const ZIP_NEARBY_RADIUS_KM = 15;/);
  assert.match(html, /const ZIP_POSSIBLE_MIN_DNI = 180;/);
  assert.match(html, /const ZIP_POSSIBLE_MIN_SCORE = 60;/);
  assert.match(html, /candidate\?\.persistence\?\.confirmed === true/);
  assert.match(html, /nearestZipCandidate\(st, finalizedGoCandidates\(artifact\), ZIP_GO_RADIUS_KM\)/);
  assert.match(html, /nearestZipCandidate\(st, newGoCandidates\(artifact\), ZIP_GO_RADIUS_KM\)/);
  assert.match(html, /New GO — look now/);
  assert.doesNotMatch(html, /trackedGoCandidates|trackedZipCard|stationDataToCandidate/);
});

test("last updated uses the detector artifact time instead of page refresh time", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /if \(generated\) markUpdated\(generated\)/);
  assert.match(html, /candidateArtifact\?\.generatedAt \? new Date\(candidateArtifact\.generatedAt\)/);
  assert.match(html, /const n = at \|\| artifactTime \|\| nowInstant\(\)/);
});

test("a local GO missing from a fresh nationwide scan remains an explained possible", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const source = functionSource(html, "applyFinalizedZipPolicy", "updateCandidateToggle");
  const context = {
    freshCandidateArtifact: () => true,
    nearestZipCandidate: () => null,
    finalizedGoCandidates: () => [],
    newGoCandidates: () => [],
    qualifiedZipPossibleCandidates: () => [],
    ZIP_GO_RADIUS_KM: 15,
    ZIP_NEARBY_RADIUS_KM: 15,
  };
  vm.runInNewContext(`${source}; result = applyFinalizedZipPolicy({
    st: { lat: 30, lon: -90 },
    verdict: { key: "go", tier: 3, sun: true, rain: true }
  }, {});`, context);
  assert.equal(context.result.verdict.key, "watch");
  assert.equal(context.result.verdict.tier, 2);
  assert.equal(context.result.verdict.localOnly, true);
  assert.match(context.result.verdict.label, /local conditions only/i);
  assert.match(html, /Local conditions look right.+nationwide scan hasn't confirmed/);

  vm.runInNewContext(`result = applyFinalizedZipPolicy({
    st: { lat: 30, lon: -90 },
    verdict: { key: "watch", tier: 2, label: "Possible - weaker sunlight", sun: false, rain: true }
  }, {});`, context);
  assert.equal(context.result.verdict.key, "watch");
  assert.equal(context.result.verdict.label, "Possible - weaker sunlight");
  assert.equal(context.result.verdict.localOnly, undefined);
});

test("forecast timestamps are parsed as UTC regardless of the viewer timezone", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const source = functionSource(html, "parseForecastTimeUtc", "buildDialData");
  const context = {};
  vm.runInNewContext(`${source}; result = parseForecastTimeUtc("2026-07-29T12:00");`, context);
  assert.equal(context.result.toISOString(), "2026-07-29T12:00:00.000Z");
  assert.match(html, /forecast_hours=24&timezone=GMT/);
});
