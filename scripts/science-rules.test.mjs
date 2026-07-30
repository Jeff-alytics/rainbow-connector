import test from "node:test";
import assert from "node:assert/strict";

import {
  SCIENCE_THRESHOLDS,
  goVerdict,
  opportunityScore,
  watchVerdict,
} from "./build-candidates.mjs";

function candidate(overrides = {}) {
  return {
    geo: true,
    sunElev: 15,
    directRadiation: 275,
    cloudCover: 55,
    rainIntensity: 0.6,
    observerRain: 0.1,
    wetSamples: 8,
    rainCloudCover: 70,
    rainDirectRadiation: 100,
    ...overrides,
  };
}

test("thresholds reflect the FAA and ARM validation envelope", () => {
  assert.deepEqual(SCIENCE_THRESHOLDS, {
    directGoMinWm2: 200,
    directWatchMinWm2: 120,
    optimalSunMinDeg: 5,
    optimalSunMaxDeg: 22,
    watchSunMinDeg: 0,
    watchSunMaxDeg: 30,
    physicalSunMinDeg: -0.833,
    physicalSunMaxDeg: 42,
  });
});

test("measured-positive-style conditions produce GO", () => {
  assert.equal(goVerdict(candidate()), true);
  assert.equal(watchVerdict(candidate()), false);
});

test("the high-sun ARM no-bow control cannot produce GO", () => {
  const control = candidate({ sunElev: 39.12, directRadiation: 308.8 });
  assert.equal(goVerdict(control), false);
  assert.equal(watchVerdict(control), false);
});

test("weaker direct sun stays possible instead of GO", () => {
  const marginal = candidate({ directRadiation: 150 });
  assert.equal(goVerdict(marginal), false);
  assert.equal(watchVerdict(marginal), true);
});

test("uniform overcast is rejected", () => {
  const gray = candidate({ directRadiation: 40, cloudCover: 96 });
  assert.equal(goVerdict(gray), false);
  assert.equal(watchVerdict(gray), false);
});

test("calibrated score favors the observed low-sun window", () => {
  assert.ok(opportunityScore(candidate({ sunElev: 13 })) > opportunityScore(candidate({ sunElev: 30 })));
});
