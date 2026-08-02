# Handoff: historical V1/V2 sunlight evaluation

## Objective

Evaluate the existing sunlight methodology on historical, pre-systematic-review cases:

1. Apply V1 and V2 to the same historical inputs.
2. Compare both outputs against independent FAA human frame labels.
3. Identify disagreements, false positives, false negatives, and useful score bands.
4. Do not modify production behavior, deploy, or write to the live review store.

Historical cutoff: `2026-07-29T00:00:00Z`. Exclude cases already reviewed in existing FAA batches and exclude frozen/tuned validation cases from the holdout.

## Important correction

The current FAA batch is only a raw candidate pool. It was selected using rain, low sun, camera alignment, and metadata scoring. It did **not** run V1 or V2 and must not be presented as a V1/V2 evaluation or rainbow-ranked review queue.

Current raw batch:

`C:\Users\jeffm\rainbow-finder\validation\faa-retrospective-pre-review-2026-07-26-28`

The current priority page is also metadata-ranked and should not be used as the final study queue:

`C:\Users\jeffm\rainbow-finder\validation\faa-retrospective-pre-review-2026-07-26-28\priority-review\faa-priority-review.html`

## Phase 1 — replayability inventory

Inspect the exact inputs required by:

- V1: `api/sunlight_decision.py`
- V2: `worker/sunlight_v2.py`
- V2 method version: `sunlight-v2-shadow-2026-07-v3`

For each historical case, identify whether archived inputs exist for:

- timestamp;
- observer latitude/longitude;
- rain geometry and candidate score;
- GOES/satellite inputs;
- MRMS/rain footprint;
- ASOS/METAR or equivalent weather inputs;
- FAA site/camera/frame metadata.

Do not substitute current weather or current satellite data. If required inputs are missing, mark the case `not_replayable`; do not evaluate it as a negative.

Stop and report if faithful replay is impossible for the available cases.

## Phase 2 — version-pinned replay manifest

Create a separate manifest under `validation/` containing:

- case ID and source;
- observed timestamp;
- location and rain geometry;
- FAA site/camera/frame references;
- V1 rule/code version;
- V2 method version;
- source artifact hashes;
- replayability status;
- split: development, calibration, or holdout.

The manifest must be deterministic and exclude:

- existing reviewed FAA cases;
- the six frozen cases;
- confirmed-bow cases used to tune the detector;
- any case previously used to choose thresholds.

## Phase 3 — replay V1 and V2

For every replayable case:

1. Run V1.
2. Run V2.
3. Give both methods exactly the same historical inputs.
4. Record full outputs, missingness, and unavailable reasons.
5. Keep human labels out of replay and threshold selection.

Each output record should include:

```text
caseId
observedAt
location
humanReviewStatus
v1:
  verdict
  sunlightCategory
  features
  methodVersion
v2:
  verdict
  sunlightState
  supportScore
  features
  methodVersion
inputs:
  artifactHashes
  replayStatus
```

## Phase 4 — blind FAA review

Use FAA images only for cases before the cutoff, in a separate local study queue. Do not call the production review API or `saveHistoricalReviewEvent`.

The review page must:

- hide V1/V2 verdicts and scores while grading;
- allow frame-level `rainbow`, `no_rainbow`, and `unusable` labels;
- save labels locally and export JSON;
- expose only review-needed metadata;
- keep human labels independent from detector output.

The final queue must be ordered using actual replay outputs, not the current metadata score. Review all high-priority and V1/V2-disagreement cases, plus a small calibration sample from lower score bands.

## Phase 5 — evaluation

After replay, stratify using actual V1/V2 outputs:

- top-scoring candidates;
- middle score bands;
- lower score bands;
- V1/V2 disagreement cases;
- missing-data or abstention cases.

Use development/calibration cases to choose any cutoff. Keep the holdout untouched until the final report.

Report separately for V1 and V2:

- precision;
- recall-like detection rate;
- abstention rate;
- unusable-image rate;
- false-positive categories;
- false-negative categories;
- score-band performance;
- V1/V2 disagreement matrix.

## Required tests

Add tests that fail if:

- V1 and V2 receive different historical inputs;
- current live weather is used during replay;
- a frozen case enters the holdout;
- human labels are exposed before grading;
- a missing archive input is treated as a negative;
- a method version changes without a manifest change;
- a case after July 29 enters the historical set;
- the review page sorts by metadata instead of actual V1/V2 output;
- production Redis or callback code is invoked.

Use mutation tests for the protections above where practical.

## Deliverables

1. Replayability report.
2. Deterministic historical manifest.
3. V1/V2 replay output.
4. Blind FAA review batch.
5. Exported human labels.
6. Separate V1 and V2 evaluation report.
7. Tests and mutation results.
8. Explicit excluded-case list with reasons.

## Requested review before implementation

Please review this scope first, especially:

1. whether the archived data can faithfully satisfy both V1 and V2 inputs;
2. whether the July 29 cutoff is correct;
3. which existing FAA batches count as already reviewed;
4. which frozen/confirmed-bow cases must be excluded;
5. whether the existing V1 and V2 functions can be called offline without production dependencies.

Do not deploy or re-enable any research lane as part of this work.

## Claude review corrections — mandatory before Phase 3

Claude reviewed the actual code paths and approved the scope with these corrections. Do not begin replay until the first three have tests.

### Correct V1 path

The production V1 baseline is the worker enrichment path, not `api/sunlight_decision.py` as a standalone unit:

- `worker/enrichment.py` builds the live Open-Meteo model features.
- `worker/decision_log.py` builds the satellite decision from GOES samples.
- `worker/sunlight_v2.py` reads those V1 fields in `v1_sunlight_category`.

The replay must reproduce the worker path so the V1/V2 disagreement matrix compares V2 with the V1 that actually ran in production.

### Required replay shims

1. **Historical METAR injection.** `enrich_sunlight_v2` currently fetches the live aviationweather cache. Inject archived IEM ASOS observations or a test session serving the same parsed schema. Record a distinct method version such as `iem-asos-archive-replay-v1`; never reuse `aviationweather-current-cache-v1`.
2. **Explicit processing time.** Pass `assessment_processing_at` for every case. Pin it to `radar_observed_at + one fixed study latency`, identical for V1 and V2, and record the constant in the manifest. It must never default to `now()`.
3. **Historical freshness.** Recompute `observedAgeMinutes` relative to the historical assessment time, not wall-clock now. Add a test that fails if replayed data appears 30 days stale and causes every V1 result to become unknown/blocked.
4. **Historical GOES selection.** Use `sample(..., observed_at=, available_by=)` rather than now-relative `latest_key` selection. Record the selection difference in the manifest.

### Open-Meteo approximation

Past `current` values are unrecoverable. Run V1 in two variants:

- satellite-only: exact archived satellite inputs;
- full: archived Open-Meteo Historical Forecast substitute, explicitly marked `approximate: true` with its own method version.

Report both variants separately and mark evaluation cells that depend on the approximate model source.

### Event-level exclusions

The July 29 cutoff remains the coarse boundary, but exclusions must use time-window and location/rain-event proximity, not only case IDs. Exclude:

- all frames in the three reviewed FAA batches: `faa-human-labels.json`, `faa-expansion-human-labels.json`, and `faa-network-expansion-human-labels.json`;
- the six frozen ledger cases: Baltimore, Colorado, Utah, Middletown, Meeker, and Delano;
- confirmed-bow/social events including Baltimore, Dundalk, Person County, and Idaho;
- neighboring cameras in the same storm/event window as those cases;
- July V2 shadow-lane disagreement cases examined during tuning.

The new July 26–28 FAA queue has zero labels and is not contaminated, but it remains an ungraded raw pool until replay-output ordering exists.

### Additional required tests

Add tests for:

- no freshness/age value derived from real wall-clock time;
- no live METAR or Open-Meteo-current method version in replay output;
- explicit `assessment_processing_at` equal to `radar_observed_at + pinned latency` for every V1/V2 case;
- production Redis, callback, and review-store code never imported or called;
- event-level exclusion catches a neighboring camera in a frozen storm, not just the exact original case.

### Retention deadline

The FAA source frames from July 26–28 are expected to age out around August 25–27. Hash the 278 downloaded local frames into the manifest immediately; the local bytes become the study archive of record. Any pool expansion for those dates must happen before that window closes.