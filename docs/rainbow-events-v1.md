# Rainbow Events v1 — review-only grouping scope

Status: **proposed**, nothing built. Independent of the ZIP coverage work; must
not touch the public verdict policy.

## What an event is, physically

This has to be settled before any schema, because it decides the grouping radius
and it is what reviewers will disagree about otherwise.

A rainbow is observer-relative. Two people 50 km apart looking at one rain shaft
under one sun are not seeing the same bow — each sees light refracted from a
different set of droplets, centred on their own antisolar point. There is no
object in the sky that both photographed.

So the thing being grouped is **shared cause, not shared object**: one rain
volume, one solar geometry window, one time window. An event is a claim that
these photographs have the same physical origin, not that they show the same
rainbow. Every downstream rule follows from that:

- attribution is per observer, because the bows genuinely differ;
- "the same event from two angles" is meaningful, "the same rainbow" is not;
- the grouping radius is bounded by rain-shaft extent, not by how far apart two
  cameras can be.

## Boundary: review-only

The event layer is additive. Original review and photo records are unchanged and
remain the evidence of record. Events reference them; they never rewrite them.

Not in scope for v1, and each is a separate decision later:

- no public exposure of groupings;
- no feedback into detector decisions or verdicts;
- no effect on alert eligibility.

## The anchoring trap in lookback search

This is the part most likely to go wrong, and the repository has been bitten by
this exact failure twice already — it is why `awaitingGrade` and
`allCameraViewsReviewed` exist in `api/go-events.mjs`.

Lookback search proposes "possible supporting views" for an event that already
contains confirmed photographs. Presenting a candidate in that context tells the
reviewer the expected answer before they have looked at it. A reviewer shown
"here are four confirmed bows, was this fifth camera also showing one?" is not
grading the fifth camera. It is the anchoring leak in a new costume.

**Rule:** lookback candidates enter the ordinary blinded review queue as
independent items, carrying no indication that they were surfaced by an event.
They join the event only after independent grading. The event card may show what
was searched and what has been returned; it may not be where a candidate is
graded.

## Circularity

The proposed grouping signals — compatible timestamps, nearby locations,
compatible sun/rain geometry, compatible bow direction — are the detector's own
feature set. That is acceptable while grouping stays review-only, but it means
**event agreement can never serve as independent evidence that the detector is
right.** Two photographs grouped because they share the detector's features
agreeing with the detector is not corroboration.

Writing this down now so nobody reaches for event agreement as a validation
metric later. If independent validation is wanted, it needs a signal the
detector does not use.

## Identity model

"One photo may not belong to two events without an explicit split" is a
constraint on a relation that has to be defined first.

- membership is a **record**, not a field on the photo: `(eventId, photoId,
  addedBy, addedAt, reason)`;
- a photo has at most one *active* membership; superseded memberships are
  retained, not deleted;
- merge produces a new event referencing both parents; split produces two
  referencing one parent. Neither mutates a parent's membership history.

That makes "preserve original evidence" and "auditable location/time changes"
fall out of the storage model rather than depending on reviewer discipline.

## Suggestion function: conservative means abstaining

A suggestion should be withheld whenever any input is missing, rather than
guessed. Specifically, no suggestion when either photo has an unresolved
location, when the solar geometry cannot be computed for both, or when the time
window depends on an inferred rather than recorded timestamp.

Precision matters more than recall here for the same reason the ZIP work went
shadow-only: a reviewer who learns that suggestions are usually wrong stops
reading them, and the feature is then worse than nothing.

## Required protections

Each needs a test that fails when the behaviour is reverted, per
`docs/agent-handoff.md`:

1. unrelated photographs sharing only a timestamp do not merge;
2. a photo cannot acquire a second active membership without an explicit split;
3. editing an event leaves the underlying review records byte-identical;
4. location and time changes append to an audit trail rather than overwrite;
5. deleting an event deletes no photographs and no review records;
6. a lookback candidate is not gradeable from within the event card;
7. a suggestion is withheld when any required input is absent.

Protections 6 and 7 are the ones worth writing first — they encode the two
failure modes above, and both are easy to lose in a UI refactor.

## Sequence

1. identity model and storage, with protections 2–5;
2. suggestion function, with protections 1 and 7;
3. review interface, with protection 6;
4. lookback search, only once 6 is enforced;
5. historical evaluation against known multi-view cases and deliberately
   similar-but-distinct cases.

Step 5 gates any discussion of public exposure or detector feedback. There is no
step in v1 that ends in a public surface.

## Open decisions

- What time window counts as compatible? Rain shafts evolve over minutes; the
  bow envelope constrains solar elevation to `-0.833 ≤ apparent elevation < 42`,
  which bounds the window differently by latitude and season.
- What separation counts as one rain volume? Should follow from the MRMS
  footprint already computed per scan rather than a fixed kilometre figure.
- Does an event have a location at all, or only a set of observer locations? The
  physical definition above argues for the latter.
