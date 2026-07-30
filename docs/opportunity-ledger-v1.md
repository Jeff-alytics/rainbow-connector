# Opportunity Ledger v1

## Purpose

The Opportunity Ledger is the exhaustive private research record beneath the
public GO and POSSIBLE tiers. It preserves every opportunity supported by the
available rain sensor so a later confidence gate can demote an opportunity but
cannot silently erase it.

The ledger does not alter public candidates, ZIP cards, alerts, or email. Email
remains a one-way GO notification channel and is never used for confirmation.

## Permanent recall vocabulary

- **Opportunity-ledger recall:** how many sensor-supported physical
  opportunities were retained internally.
- **Public POSSIBLE recall:** how many known opportunities were surfaced as
  Possible - check your sky, subject to a volume budget.
- **GO recall:** how many known opportunities reached the precision-protected GO
  tier. It must never be presented as equivalent to ledger recall.

## Records

### Rain event

A scan-local connected component contains every native MRMS cell at or above
0.05 mm/hr, including heavy cores. Components receive deterministic IDs and are
linked between scans through explicit continuation, split, and merge edges. A
stable event ID groups their lineage without discarding the scan-local IDs.
Each edge records native-cell overlap or a bounded motion-fallback gap. At a
merge, the stable identity follows the parent with the largest real overlap,
and that primary edge is explicit so a tiny radar speck cannot rename a large
tracked event.

### Bow opportunity

One bow opportunity links a rain component to its complete physically viable
land-observer swath at the radar timestamp. Creation uses rain and solar
geometry only. Missing sunlight inference, cameras, model data, or publication
capacity cannot prevent this private record from existing.

### Observer swath

The scientific record is a fixed CONUS grid with 0.025-degree cells, encoded as
deterministic row runs. Its nominal spacing is about 2.8 km north-south and
1.8-2.5 km east-west across CONUS, satisfying the v1 placement tolerance of 3
km. A display polygon may be derived later; the grid remains authoritative for
containment tests.

The observer-swath-physical-envelope-2026-07-v3-viable-only numerical method
batches the same great-circle ray projection with vectorized floating-point
operations. Every ledger stores both that method version and
vectorized-great-circle-v1, so numerical reproductions never mix silently.

Rain events remain in the scan ledger even when the Sun is outside the
physical bow envelope. A bow-opportunity record is emitted only when its
observer swath contains at least one viable land cell; empty swaths are not
mislabelled as opportunities.

For v1, observer cells are generated from rain-edge samples no more than about
2.5 km apart across the full
elevation-dependent primary-bow envelope, at rain distances from 5 to 40 km.
The observer may be wet: observer rain is evidence quality, not an opportunity
creation veto. Terrain is not a v1 gate and will arrive first as a shadow
feature.

### Representative candidate

Representative points are a ranked and budgetable derivative of the swath.
They exist for maps, ZIP matching, review, and later alerts. Their spacing must
never define whether the underlying opportunity existed.

## Camera evidence boundary

- A camera no farther than 40 km from a representative observer may contribute
  candidate-level positive or quality-qualified negative evidence.
- A positive camera farther than 40 km may corroborate only the rain event.
- A negative camera farther than 40 km is ignored.
- Cameras never participate in rain-event or observer-swath generation.

## Fail-open dispositions

Every inference input records one of: supported, disproved, unresolved,
invalid, stale, or not_evaluated. Only affirmative physical disproof can remove
a location from a later public tier. Invalid or missing products remain visible
in the private ledger.

## Schema

The top-level envelope is opportunity-ledger.v1 and contains scan time, rain
footprint ID, method version, grid tolerance, rain events, bow opportunities,
lineage edges, and summary statistics. Each opportunity contains its scan
component ID, stable event ID, solar/rain threshold snapshot, encoded observer
swath, swath statistics, and derived representative candidates. Camera and
sunlight assessments attach later by IDs.

## Proof gate

Before production shadow storage is wired, the implementation must replay the
frozen Baltimore, Colorado, Utah, Middletown, Meeker, and Delano windows and
answer separately:

1. Was the rain event retained?
2. Was a bow opportunity retained?
3. Was the reported observer inside the swath within 3 km?
4. Was a representative candidate nearby?
5. Did sunlight inference or another later stage demote it?
6. Did distant positive cameras attach only to the event?
7. Were distant negative cameras ignored?

The proof must report failures rather than tune geometry until every named case
passes. Public output and email behavior remain unchanged throughout.
