# ZIP Coverage v1 — Phase 0 scope

## The defect being fixed

A ZIP is currently matched to the nearest sampled candidate within 15 km
(`ALERT_RADIUS_KM` in `api/notify-alerts.mjs`). That answers "is there a sampled
candidate near you", not "are you inside the area where the conditions apply".
The two differ in both directions: a location genuinely inside an observer swath
gets nothing when no representative pin landed within 15 km, and a location 5 km
from a pin but outside its swath is alerted anyway.

Candidate pins stay useful map representatives. They stop defining eligibility.

## Architecture: reverse point query, ledger stays private

The opportunity ledger computes **forward** — from every rain edge, project all
viable observer positions, 337k–522k cells across CONUS. That is the right shape
for an exhaustive private record and the wrong shape for this.

Coverage asks the **reverse** question for a small set of fixed points: for this
observer, is there rain at 5–40 km in the anti-solar direction with the sun in
the bow envelope? That is a bounded ray sample per point, computed in the main
worker. No new async artifact, no email latency, and the ledger keeps the private
boundary stated in `opportunity-ledger-v1.md`.

Prerequisite already met: RainbowWorker was raised to 3008 MB after peaking at
1945 MB of a 2048 MB ceiling.

## Two stages, because sunlight is not free

Measured cost per point:

| Stage | Inputs | Per-point cost |
| --- | --- | --- |
| Geometry | solar position, bow envelope, rain grid, land mask | microseconds, vectorised |
| Sunlight | Open-Meteo + GOES | **~1 HTTP request per 10 points** |

`batch_open_meteo` in `worker/enrichment.py` is hard-capped at `batch_size = 10`,
and the production rule runs it over a *shortlist*, never all candidates. So
geometry may run over any number of points; sunlight may not.

## What the geometry-pass spike measured

41,488 ZIP points from `zip-centroids.json`, three real cached MRMS scans from
`.worker-cache/opportunity-ledger-mrms`, both containment variants, offline with
no network and no Open-Meteo calls.

| Scan | Wet cells | Sun in envelope | Point containment | Disc (4.2 km) |
| --- | --- | --- | --- | --- |
| quiet | 587,551 | 16.3% | 451 (1.09%) | 273 (0.66%) |
| moderate | 617,570 | 22.2% | 878 (2.12%) | 606 (1.46%) |
| stormiest | 644,102 | 40.1% | 1,623 (3.91%) | 1,331 (3.21%) |

Projected Open-Meteo requests at `ceil(passing / 10)`: 46 / 88 / 163 per scan for
point containment. At 288 scans/day that is 25,000 (median) to 47,000 (worst)
requests/day against a 10,000/day free tier.

**These are lower bounds.** The cached scans span only 587k–644k wet cells, so
none is genuinely quiet. Solar eligibility tops out at 40%, so the 94–100% peak
window is unrepresented — at peak eligibility with comparable rain, passes would
plausibly be 2–3x higher. Sampling was coarse at 7 bearings x 8 distances, and
finer sampling finds more rain, not less.

## The scope correction

Nothing requires coverage for all 41,488 ZIPs every scan.

- **Emails** need only the subscribed ZIPs. There are currently **9**. One
  Open-Meteo request per scan.
- **ZIP cards** resolve on demand when a visitor enters a ZIP: one point, one
  request, cacheable for the scan's lifetime. Not per-scan work.

The earlier plan pre-filtered by geometry ("only ZIPs intersecting active
opportunity bounds"). The sharper filter is **subscription**. The spike above
measured a workload nobody needs; the architecture survives, the scope was wrong.

Free tier binds at roughly **350 subscribers** (35 requests/scan). Past that it
is a paid API tier, not a redesign.

## Containment policy: uncertainty disc

Settled by the spike. GO requires the location's **uncertainty disc** to be
contained, not just its centre point:

- disc fully inside a viable swath with sunlight supported -> **GO**
- disc intersects the swath, or sunlight is unresolved/invalid/stale -> **POSSIBLE**
- disc fully outside -> **none**
- coverage missing or stale for this scan -> **unavailable**, and no email

The disc cut passes 30–40% versus point containment across all three scans, which
is the conservative direction for GO. It also makes GO automatically strict for
coarse ZIP centroids and precise for user-pinned points, which turns "pin your
location" into a feature rather than a friction tax.

Uncertainty must demote to POSSIBLE, never to "no opportunity".

## Privacy boundary

AWS receives unique watched location IDs. It never receives email addresses.
Vercel keeps the email-to-location mapping and performs delivery. Emails remain
one-way "look now" notifications and never request confirmation, which means
subscriber locations are unvalidatable by design — a further reason GO stays
conservative under uncertainty.

## Shared policy module

One pure function, `api/zip-verdict-policy.mjs`:

```
evaluateWatchedLocation({ watchedLocation, candidateArtifact, coverage, now })
  -> { verdict, reason, opportunityId, candidateId, lookDirection, scanTime, ruleVersion }
```

Both the ZIP-card path and `api/notify-alerts.mjs` call it. Purity is what makes
the reproducibility requirement testable: same inputs, same verdict.

## Watched location record

`lat`/`lon` already exist on subscriber records (`notify-alerts.mjs` uses them),
so this is additive metadata, not a schema migration, and existing subscribers can
be backfilled:

```json
{ "zip": "70124", "locationMethod": "zip-population-centroid-v1",
  "lat": 30.007, "lon": -90.105,
  "locationUncertaintyKm": 4.2, "locationVersion": "us-zip-points-2026-v1" }
```

`zip-centroids.json` holds *geographic* centroids. Population-weighted ZIP points are
in `data/zip-location-points-v2.json`, derived from HUD residential ratios and
Census 2020 tract centers. Each weighted ZIP carries a computed uncertainty radius
with a 4.2 km floor; weighted dispersion above 25 km requires a user pin. Shared
coordinates and ZIPs without positive residential ratio also require a pin. The current artifact marks 8,364 ZIPs as pin-required. Signup must detect those and prompt for a point rather than accept a ZIP it cannot resolve.

## Open decisions

1. Does raw swath membership surface as public POSSIBLE immediately, or
   shadow-only first? Recommend shadow-only: POSSIBLE is what most users see most
   often, and a frequently-wrong POSSIBLE devalues GO.
2. The disc sampling density used to test containment. The v2 data artifact now
   supplies a per-ZIP uncertainty radius and pin-required flag.

## Sequence

1. ZCTA population-weighted centroid ingestion, and the PO-box-only signup branch.
2. Coverage for watched locations in the main worker, geometry then sunlight.
3. `api/zip-verdict-policy.mjs` as a pure function, shadow-only.
4. Compare against the 15 km rule; log disagreements; change nothing user-facing.
5. ZIP cards to the new policy for POSSIBLE.
6. Unify GO across cards and email behind the shared module.
7. Delete `ALERT_RADIUS_KM`.

## Gates before step 5

Replay must retain at least POSSIBLE for Baltimore/Dundalk, Silver West,
Saguache, Fremont County, Ogden, Bear River, Middletown, Meeker, Delano, and the
70124 over-broad case. GO precision must not worsen on 70124. Alert volume per
subscriber-day must be measured by evaluating all 41,488 ZIP points, not the 9
current subscribers, so the volume signal is meaningful. Stale or missing coverage
must provably send zero emails.

## Required tests

1. A ZIP inside a swath can be POSSIBLE with every representative pin beyond 15 km.
2. A ZIP 5 km from a pin but outside its swath is not matched.
3. A GO elsewhere in an event does not promote the watched point.
4. A location-level GO produces the same card and email decision.
5. A location-level POSSIBLE never sends a GO email.
6. Mismatched scan timestamps cannot be combined.
7. Missing or stale coverage sends no email and does not erase map pins.
8. Large-ZIP partial overlap does not create a GO.
9. Cooldowns key on location + opportunityId + ruleVersion.
10. Card and email decisions are reproducible from stored artifacts.
11. A location's verdict is invariant to other locations in the same event.
12. The policy module is pure: same inputs, same verdict.

## Out of scope

Both research lanes stay disabled: `RAINBOW_RESEARCH_REVIEW_ENABLED=false` on
SunlightShadowWorker, no review callback URL on OpportunityLedgerWorker. The
opportunity ledger stays private and is not promoted to a production artifact.
