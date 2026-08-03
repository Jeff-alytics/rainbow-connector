# Storm-object replay v1

This iteration replaces scan-to-scan observer-seed identity with the existing
Opportunity Ledger's MRMS component lineage. It does not change the frozen Gate
3 criteria and it cannot alter public candidates or alerts unless the gate
passes and Claude independently reproduces the result.

## Identity

The rain-footprint sidecar now retains exact 0.5 mm/hr envelope and 2.0 mm/hr
core runs in addition to its existing lossless rate tiers. Eight-connected
cores define identities. Envelope rain is assigned to the nearest core by
Chebyshev cell distance with a deterministic lexical tie-break. This retains
light rain around a storm without allowing a light stratiform bridge to weld
two core storms into one object.

The existing ledger continuation/split/merge linker owns temporal identity.
Split children inherit the same family. Merges follow the largest-overlap
parent for stable display identity while retaining the union of every parent
family in ancestorEventIds. Alert state is keyed to that ancestry, so splits
and merges cannot manufacture fresh alerts.

## Candidate attachment and scoring

The camera-independent fixed-CONUS probe seeds remain the observer geometry
input. A seed attaches only when its rain target cell lies inside a current
storm envelope. Persistence, current radar, and causal peak radar are then
aggregated at storm-object level. Spatial support remains required; sunlight is
a modifier only and missing sunlight remains neutral.

## Scheduler

The amended scheduler is frozen before real replay in
validation/storm-object-replay-gate/gate-config-v2.json. It is causal: a
three-token bucket refills at ten tokens per 24 hours with no clock-time reset.
At each scan it selects the highest-ranked currently
eligible, never-alerted ancestry. It does not look ahead or rerank at day's end.
Natural pre-scheduler volume and token-starved eligible objects are reported so
the scheduler cannot conceal a poor detector.

## Label matching

The primary label-to-family assignment uses the target rain coordinate and the
ledger's physical observer swath at the nearest scan. Once assigned, any alert
on the object's split/merge ancestry counts. The old two-kilometer seed match is
reported only as a diagnostic; it no longer defines storm identity.
