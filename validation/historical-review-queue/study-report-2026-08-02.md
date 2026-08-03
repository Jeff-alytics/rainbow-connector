# Historical FAA study — V1 evaluation report (interim: V2 comparison pending)

2026-08-02. Single blind reviewer (Jeff), three rounds, one season (July 3–28, 2026).

## Pipeline

Archived MRMS (noaa-mrms-pds) → production `observer_seeds_from_rain_grid` (stride 10,
max 400/scan) at 10-min cadence over 3,568 sun-eligible frames → production
`camera_matches` (40 km, bow-facing wedge) → 4,652 site events → event-level exclusions
(frozen cases + reviewed batches, time+radius) → V1 replay per event best-scan
(archived GOES exact; Open-Meteo historical-forecast substitute flagged approximate)
→ production disposition constants → blind review of every retrievable queue event.

## Sample

- 4,513 events replayed, 0 replay errors. Queue: 50 go / 1,255 possible / 146 near-miss.
- 1,240 queue events had retrievable FAA frames and were blind-reviewed (211 aged out
  of FAA's ~30-day archive; recorded unretrievable, never negatives). 1 unusable.
- 29 events human-confirmed to contain a rainbow.

## Results (event-level precision, after machine-assisted corrections)

35 confirmed rainbow events: 29 found blind across three rounds + 6 recovered in the
machine-disagreement re-review (reviewer confirmed each; recorded in
human-labels-corrections-2026-08-02.json). Single-pass reviewer sensitivity ≈ 29/35
(83%); rates below are lower bounds since only machine-flagged events got a second look.

| Production tier | Rainbows | Rate |
|---|---|---|
| go (strict)     | 1 / 42   | 2.4% |
| possible        | 28 / 1,065 | 2.6% |
| near_miss       | 6 / 132  | 4.5% |

| Persistence band | Rainbows | Rate |
|---|---|---|
| 13+ scans        | 16 / 121 | 13.2% |
| 5–12 scans       | 14 / 384 | 3.6% |
| 1–4 scans        | 5 / 734  | 0.7% |
| 13+ scans & radar ≥ 95 | 15 / 86 | 17.4% |

Notable: the single GO rainbow (Prospect Peak, 14 scans, radar 99.5) is the only GO
event that sits in the persistent/high-radar band — the exception that follows the
rule. Pre-correction blind-only tables (GO 0/42) preserved in git history of this file.

## Findings

1. **Tier ordering inverted.** GO (2.4%) performs no better than possible (2.6%) and
   worse than near-miss (4.5%), while the persistence band reaches 17.4% — ranking by
   the current GO gate is far worse than ranking by persistence, and GO's one hit is
   itself a persistence-band event.
2. **Persistence is the dominant signal.** Monotonic 11.6% → 3.1% → 0.4% across scan
   bands. Radar score compounds it.
3. **Mechanism.** Satellite "go" requires DSRF ≥ 200 corroborated by ACMC clear at the
   observer pixel. Bow light at 5–22° sun arrives under the cloud deck from near the
   horizon; the overhead mask vetoes exactly the persistent organized storms that
   produce bows (GO median 3 scans vs queue median 4; only 1/50 GO events reached the
   13+ band). The mask measures the wrong part of the sky for this use case.
4. **V2's Band-2 sunward-gap feature (2/4/6 km displacement toward the sun) asks the
   right question**; testing it against these labels is the next stage.

## Integrity notes

- Blind protocol: shuffled page, no tiers/scores shown. GO 0/12 from rounds 1–2 fully
  blind; ~28 GO site names were disclosed to the reviewer before round 3, so round-3 GO
  grades are flagged unblinded (disclosure recorded in study-notes.json). Result was
  0-for-GO in both the blind and unblinded subsets.
- Exclusions: six frozen ledger cases (event-level, time+radius — verified to catch
  neighboring cameras), three previously reviewed FAA batches, July shadow-tuning cases.
- Label conventions per round recorded in reviewed-rounds.json. Round-level exports:
  human-labels-round{1,2,3}-2026-08-02.json.
- Approximation: DNI in dispositions comes from Open-Meteo Historical Forecast
  (methodVersion open-meteo-historical-forecast-replay-v1, approximate: true); GOES
  lanes are exact archived data with causal cutoffs (radar time + 8 min).
- Machine reviewer (CLIP contrastive, sealed until human labels locked):
  machine-reviewer-clip.jsonl. Performance vs human labels: at prob ≥ 0.5 it flags 42
  events and catches ~62% of blind-confirmed bows, but scores several faint confirmed
  bows below 0.03 — usable as a second-reviewer net, disqualified as a gatekeeper.
  Disagreement pass (top 20 machine-high/human-no) recovered 6 real bows
  (machine-disagreements.html; corrections file above).

## Caveats

Single reviewer; single month/season; solar-geometry regime inverts by season; frame
sampling at ~8–10 min cadence can miss short-lived bows; 211 events unreviewable due to
archive retention. No production changes made; any GO-gate re-weighting goes through
the standard implement/verify pipeline.

## Addendum (2026-08-03): V2 replay + frozen-case ranking check — COMPLETE

### V2 sunward-gap vs labels (136-event stratified subset)

| role | sunlit | plausible | unresolved | overcast (drop) | unavailable |
|---|---|---|---|---|---|
| confirmed bows (35) | 24 | 3 | 4 | 2 | 2 |
| go_no_rainbow (41)  | 41 | — | — | — | — |
| controls (60)       | 46 | 2 | 4 | 4 | 4 |

- On confirmed bows: V1 satellite "go" ≈ 0/35; V2 sunlit-or-plausible 27/33 available
  (82%). V2's positive calls human-validated (21/21 adjudicated go_no_rainbow events
  visibly sunlit).
- Both V2 affirmative false negatives (Choteau, Colstrip — morning sliver bows) had
  ZERO METAR stations, and `sunlight_state` counts METAR *absence* as
  overcast-consistent. **Refinement R6:** require present METAR evidence (score ≤ 0.3)
  for `overcast_supported`; absence → `unresolved`. With R6, V2 affirmative false
  negatives on bows = 0/33.
- Two more genuine DSRF outages hit the subset (Jul 5–6, Jul 15 21Z+): data-gap rows.

### Frozen-case check: would persistence-first ranking have caught the live misses?

Camera-free production seeds regenerated at 10-min steps around each frozen case
(window ±90 min, seeds within 60 km of the observation):

| case | persistence | peak radar | peak timing vs bow |
|---|---|---|---|
| Baltimore/Dundalk 7/28 | 15/15 scans | 99.9 | ~20 min BEFORE bow window |
| Colorado multicam 7/30 | 15/15 scans | 100.0 | ~25 min before |
| Utah Ogden 7/30        | 12/14 scans | 100.0 | ~50 min before |

CORRECTED per Codex review (2026-08-03): reproducible artifact now at
validation/frozen-case-pool-check/camera-free-seeds.json (script:
scripts/frozen_case_camera_free_check.py). Utah's 12/14 must NOT be described as the
13+ band; windows truncate lineage history; and per-scan presence in a fixed radius is
a bounded diagnostic, not a causal replay. The defensible claim: all three cases show
sustained multi-scan seed presence with radar peaking 95+ BEFORE the observed bow —
consistent with, but not yet proof of, prospective lead time. The causal per-scan
replay (Codex methodology proposal) is the correct instrument for the lead-time claim.

### Causality correction (Codex review, independently verified)

Completed-event scanCount is not knowable at alert time. Among 28 blind positives with
timestamped rainbow frames, the first marked bow frame occurs at median 3.5 accumulated
scans (23 had ≥2, 20 had ≥3, 12 had ≥5, NONE had 13) — and marked frames can only
postdate bow onset, so onset is earlier still. A 13-scan alert threshold would fire
after the show. Exposure-adjusted event rates (rainbow EVENTS divided by frame
exposure — the reviewer marked representative, not exhaustive, bow frames, so these
are not true per-frame probabilities): 13+ band 1.31%, 5–12 band ~0.48% (including the
event-only-labeled positive), 1–4 band 0.35% — the gradient is real (~3x) but smaller
than the per-event 7x. The planned per-scan hazard analysis requires
interval-censored onset labels: the first marked frame is only an upper bound on bow
onset. R1 is accordingly reframed: persistence enters as an ACCUMULATING CAUSAL
feature saturating around scans 3–5, combined with causal-to-date radar and spatial
support — never completed scanCount × peak radar. July 3–30 and all six frozen cases
are a development set; thresholds must be frozen before any holdout evaluation.

### Final recommendation set

R1 persistence×radar ranking; R2 sunlight as permissive gate (V2 affirmative-overcast
drop only); R3 echo spatial-support gate (+RQI, ACMC-contradiction rain sanity);
R4 evidence capture keyed to persistence, quality camera networks only (AlertWest
incoming); R5 standing monthly harvest; R6 METAR-presence requirement for V2
affirmative-dark. None implemented; all subject to independent review.
