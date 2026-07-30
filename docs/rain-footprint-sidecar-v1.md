# MRMS rain-footprint sidecar v1

## Purpose

The sidecar preserves the rain region used by the detector so camera geometry and later experiments do not have to reduce rain to one point or re-download historical radar.

There is one sidecar per MRMS scan. Candidates link to it by `rainFootprintId`; rain cells are never duplicated per candidate.

## Grid encoding

The sidecar records the source grid geometry and lossless row runs for every finite cell with precipitation at least 0.05 mm/hr.

Three rate tiers are stored:

- tier 1: 0.05 to less than 1 mm/hr;
- tier 2: 1 to less than 5 mm/hr;
- tier 3: at least 5 mm/hr, including cores above the detector's current 20 mm/hr seed limit.

Each run is `[rowIndex, firstColumnInclusive, lastColumnInclusive, tier]`. Adjacent cells are combined only when their row and tier match. The JSON object is gzip-compressed in storage.

```json
{
  "schemaVersion": "mrms-rain-footprint.v1",
  "rainFootprintId": "mrms-footprint-20260728T230200Z",
  "observedAt": "2026-07-28T23:02:00Z",
  "sourceKey": "CONUS/PrecipRate_00.00/...",
  "grid": {
    "latitudeCount": 3500,
    "longitudeCount": 7000,
    "latitudeStart": 54.995,
    "latitudeStepDeg": -0.01,
    "longitudeStart": -129.995,
    "longitudeStepDeg": 0.01
  },
  "rateTiersMmHr": [0.05, 1, 5],
  "runs": [[1200, 3301, 3312, 2]]
}
```

The exact grid metadata is read from the MRMS frame; the example dimensions are illustrative and must not be hard-coded.

## Candidate-derived anti-solar rain span

The definition is fixed before implementation:

1. Use the candidate observer point and anti-solar bearing at the radar timestamp.
2. Consider wet-cell centers between 5 and 40 km from the observer.
3. Keep directions within 50 degrees on either side of the anti-solar bearing.
4. Bin retained directions into one-degree azimuth bins.
5. Fill internal gaps no wider than two degrees to tolerate the discrete grid and small radar holes.
6. `antiSolarRainArcSpanDeg` is the width of the largest contiguous occupied run.
7. `antiSolarRainOccupiedDeg` is the total number of occupied degrees across the 100-degree sector.
8. `antiSolarRainSegmentCount` is the number of occupied runs after gap filling.

Record these three values for any-rain, tier-2-plus, and tier-3 rain. No rain-span value may eliminate a candidate until it has been evaluated against both the positive and weighted-negative fixtures.

Candidate decision records retain the any-rain values in the existing flat fields and store all three tiers under `antiSolarRainSpanByTier` with keys `any`, `tier2Plus`, and `tier3`. Each tier contains `arcSpanDeg`, `occupiedDeg`, and `segmentCount`.

This is a two-dimensional surface-radar feature. It does not assert a rain-top height and must not be described as proof that every backed ray contains liquid rain aloft.

## Storage

- Rolling prefix: `rain-footprint/rolling/YYYY/MM/DD/`.
- Rolling retention: 14 days.
- Frozen prefix: `rain-footprint/frozen/`, copied with the corresponding decision logs when a confirmed event or research annotation intersects the scan.
- Sidecars are private research data, not browser payloads.

## Shared camera geometry contract

The camera-availability check and Phase 1 must call the same matcher.

Inputs:

- event/candidate location, rain-footprint link, and predicted window;
- exact frame capture timestamp in UTC;
- camera position;
- camera bearing and horizontal field of view when known;
- cached view-cluster sky mask and quality metadata when known.

Outputs include:

- time offset and timestamp validity;
- distance to candidate;
- visible bow fraction;
- rain-backed visible bow fraction;
- bearing status (`known`, `unknown`, or `stale_view_cluster`);
- geometry reason and evidence-quality components.

Unknown bearing uses a conservative degraded result for positive discovery but produces zero negative-evidence weight. Camera proximity alone is never a substitute for this matcher.

## Operational acceptance budget for fallback review

Before examining experimental outcomes, a broadened fallback must satisfy all of these over at least seven representative days:

- no regression on any frozen confirmed-positive recall case;
- fallback candidates may occupy at most two review slots in any scan;
- fallback-dominated review queues must occur in at most 5 percent of scans over the evaluation window;
- the revised rule must cover the corrected Baltimore/Dundalk observer-reported replay window, 2026-07-28 23:25-23:45 UTC;
- there may be no measured weighted-precision regression on the negative fixture;
- public GO classification and email eligibility remain byte-for-byte unchanged unless a later, separately approved experiment changes them.

These are review-capacity limits, not meteorological thresholds. They may be revised before an experiment is run, with the reason recorded, but never after looking at that experiment's answer.
