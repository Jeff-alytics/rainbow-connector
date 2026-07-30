# Sunlight v2 shadow plan

## Objective

Estimate whether direct sunlight reaches each candidate observer without changing the existing detector outcome. At Baltimore's corrected observer-reported bow time, ACMC read cloudy and the DSRF retrieval was outside its good-quality solar-zenith range at both the city and nearby detector seeds; exact rejected-seed model DNI was not retained. Sunlight v2 therefore adds independent evidence rather than merely lowering existing thresholds.

All v2 values are shadow features until the pre-registered release test passes. They cannot create a public GO, alter ZIP cards, or send email.

## Immediate-recognition evidence stack

### GOES ABI Band 2 visible imagery

- Use the 500-m, five-minute visible product from the same NOAA AWS data family.
- Sample sunward-displaced boxes using multiple plausible cloud heights divided by `tan(solar elevation)`.
- Record gap fraction and mean reflectance for point, neighborhood, and displaced samples.
- Normalize Band-2 radiance to top-of-atmosphere reflectance using radiance divided by `cos(solar zenith) * band solar constant`. This cosine correction is required so thresholds transfer across the low-sun range. Record the solar constant and normalization version used.
- Use only frames that existed by assessment time. Within the trailing 15-minute research window, summarize area-averaged gap evidence with the 75th percentile and require corroboration across frames rather than trusting a noisy maximum.
- Version the displacement scenarios and radiometric normalization method.

Implementation v3 uses the file-provided `kappa0` coefficient to convert radiance to reflectance factor, then records a separate cosine-normalized value. Each spatial sample is a 10-by-10-pixel box with at least 80 valid pixels. Band 2 is unavailable below 2 degrees solar elevation. Every consulted file key, scan timestamp, creation timestamp, actual offset, and frame count is retained.

Shadow method v3 enforces causality using source availability, not merely observation time. A GOES file is eligible only when its filename creation timestamp is no later than the assessment processing time. A METAR observation is eligible only when its observation time is no later than that cutoff. Later data is excluded rather than retroactively improving a real-time assessment. A successfully enriched decision log is immutable for that method version.

### ACMC four-level mask

Retain clear, probably clear, probably cloudy, and cloudy. Log point and neighborhood category fractions instead of collapsing the product to one Boolean.

### METAR/ASOS

- Use the nearest one to three stations within 30 km.
- Record station distance, time offset, cloud layers, visibility, and weather codes.
- FEW/SCT low layers are sun-shower-compatible positive context.
- BKN/OVC or missing observations cannot veto a candidate because the station may not share the observer's gap.

### Camera illumination

When a timely camera frame lies within approximately 10 km, hard shadows or direct highlights provide strong positive sunlight evidence. Their absence is never negative evidence because the surface, view, and exposure may be unsuitable.

The review interface includes a one-click `Scene visibly sunlit?` annotation before the rainbow grade. Model details remain hidden until after grading so the human label is not anchored by v2. It has no operational effect.

### Existing DSRF and model DNI

Keep raw DSRF and DNI as supporting features. Read and retain the DSRF pixel's `DQF` and the product file's quantitative solar-zenith bounds. A DSRF value is usable only when `DQF` marks it good and the solar zenith is inside those quantitative bounds. The current GOES-19 files declare 0-70 degrees as the good-quality range; higher-zenith retrievals are degraded or invalid and must be represented as unavailable, never dark and never negative evidence. Store the bounds and rule version rather than hard-coding an undocumented cutoff.

Only for quality-valid DSRF, add clear-sky expected DSRF and `DSRF / clear-sky expected DSRF`, with the method version recorded. When DSRF is invalid or the expected denominator is too small, log a null ratio plus an explicit reason. Baltimore demonstrates that normalization cannot rescue a retrieval made outside the product's quantitative range.

## Shadow output

Each candidate decision record receives:

- `sunlightV2MethodVersion`;
- provisional `directSunProbability` from 0 to 1;
- every component feature before combination;
- component availability and freshness;
- DSRF `DQF`, solar zenith, quantitative bounds, usability, and an explicit unavailability reason;
- the existing v1 sunlight decision for comparison;
- no operational disposition derived from v2.

It also receives one of four evidence-basis states: `sunlit_supported`, `sunlight_plausible`, `unresolved`, or `overcast_supported`. Only the final state is affirmative dark evidence. Missing, invalid, late, or conflicting inputs produce `unresolved`, never dark.

Selected GO, selected POSSIBLE, and budget-selected research POSSIBLE assessments are sent to the private Review Workbench through a dedicated signed callback. The callback is idempotent, capped at 128 KiB, and carries compact features plus exact source identifiers. Its secret lives in SSM Parameter Store on AWS and a dedicated Vercel environment variable. Callback failure is visible in the shadow result but cannot affect public publication or email.

The evaluation report must include the headline metric `v1V2DisagreementRateByDisposition`: the fraction of selected GO, selected POSSIBLE, and rejected records where the shadow-v2 category disagrees with the existing v1 sunlight category. Report record counts and missing-v2 counts beside every rate.

Raw features must accumulate before the probability combiner is calibrated. Provisional hand weights may help research displays but cannot drive production classification.

## Evaluation

Accumulate at least two weeks of shadow records, then evaluate:

The shadow period also grows a separately frozen negative-fixture v1.1 using the same weighting methodology. Precision comparisons report confidence intervals as well as point estimates; the current v1 fixture has only 21.248 total calibration weight and cannot reliably resolve subtle regressions.

- recall on the 13 camera-confirmed Audit B events;
- coverage of Baltimore/Dundalk replay windows;
- Person County and Idaho as region/window probes, not threshold-fitting points;
- weighted precision on the content-hashed negative fixture;
- missing-data and outage behavior by source;
- camera-review workload under the pre-registered fallback budget.

## Sequence

1. Approve the candidate-decision-log schema and private storage plan.
2. Logging and the weighted negative fixture are implemented; preserve their versioned schemas while evidence accumulates.
3. Implement the MRMS rain-footprint sidecar and rain-span fields.
4. Start Band 2, ACMC, METAR, quality-gated DSRF-ratio, and camera-lighting ingestion in parallel, writing shadow features into the same decision records.
5. Use the single Phase 1 camera-perspective matcher for camera availability and evidence quality.
6. Run broadened fallback experiments offline.
7. Consider production changes only after the registered acceptance criteria pass.

Band-2 and other expensive shadow analysis run in a separate asynchronously invoked Lambda after the live artifact, notifications, rain footprint, and base decision log are complete. The detector passes only the private S3 decision-log key. The shadow worker has no publishing or notification credentials and can only read and update rolling decision logs. Per-run GOES downloads are deleted after enrichment so warm containers cannot exhaust temporary storage.

## Carried-forward display backlog

The confirmed-rainbow gallery link and bow-direction wedge remain independent, low-risk public display improvements. They do not block the scientific work above.
