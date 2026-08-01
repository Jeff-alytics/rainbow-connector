# Opportunity Ledger — production safety handoff

**Date:** 2026-07-30, verification update 2026-08-01
**Author:** Claude, review requested by Jeff, addressed to Codex
**Scope:** the Opportunity Ledger changeset and the 32 repair commits that followed

---

> ## ⚠ STATUS UPDATE — 2026-08-01
>
> **Sections 1–10 are HISTORICAL.** They describe the pre-repair state at commit `1115004`
> ("checkpoint deployed research pipeline before safety repairs"). Since then, 32 repair
> commits landed on `safety/ledger-review-repair` and were **deployed to production on
> 2026-07-31** (`v20260731-callback-logging-v1`). Most §3 findings are fixed and verified.
>
> **Read §11 for the current verified state, the one remaining correctness blocker (N1),
> and the fix list.** Do not work from §3 or §8 — they are the starting point, not the
> to-do list.

---

## 0. Read this first

The ledger-derived Review lane is **live in production right now** and has **never run a scan
in its expensive regime**. The first one lands tonight around 23:30Z. Everything else in this
document is secondary to that.

Nothing in this review has been fixed. No files were modified. This is analysis only.

---

## 1. Situation

### What is deployed

Production runs the **uncommitted working tree**, on both platforms. This was verified, not assumed:

- `aws cloudformation get-template --template-stage Original` differs from the working-tree
  `template.yaml` by exactly one trailing blank line, and from HEAD by **+68 lines**.
- Live `review.html` MD5 `dda62c5f2487d7d8d8ac9c3a1127c308` == working tree; HEAD is `3f078c27…`.
- Live `index.html` MD5 matches the working tree; HEAD differs.

| | |
|---|---|
| Ledger Lambda | `rainbow-connector-worker-OpportunityLedgerWorker-VpDDJ5tLEsS7` |
| Region / account | us-east-1 / 159275356990 |
| Image | `…/rainbow-connector-worker:v20260730-ledger-review-v1` |
| Lambda updated | 2026-07-30 19:19:12Z (14:19 CDT) |
| Vercel prod | `dpl_BnayP1D2TYeTVe5hAJuCMaFkzZne`, 14:25 CDT → therainbowconnector.com |
| Kill switch | `RAINBOW_OPPORTUNITY_LEDGER_FUNCTION` **is set** — the lane is live |
| CloudWatch alarms | **none, account-wide** |

**Operational risk independent of every defect below:** production runs code that exists in
exactly one place — Jeff's working directory. No commit, tag, or branch reproduces it. If that
tree is lost or reverted, production becomes unreproducible. Commit it to a branch before
anything else.

### Why tonight matters

The ledger's solar gate (`worker/opportunity_ledger.py:323`) admits rain edges where apparent
solar elevation is in `[-0.833°, 42°)`. Computed with the repo's own `solar_position` and
`apparent_solar_elevation` over a CONUS lattice, 2026-07-30:

| Time | CONUS eligible | Production status |
|---|---|---|
| 19:20Z | **0.0%** | the 4.45 s cold start — zero swath work |
| 20:50Z | 22.3% | latest observed run: 17.6 s, 707 MB |
| 22:00Z | 51.8% | not yet run |
| **23:30Z** | **93.8%** | **not yet run — peak** |
| 00:30Z | 80.4% | not yet run |
| 01:30Z | 54.5% | not yet run |

All 28 production invocations fell between 19:19Z and 20:52Z — the 0%–22% band. Local
measurement of `build_opportunity_ledger` on real cached full-CONUS MRMS at 22:16Z and 01:20Z
gave **108.3 s and 104.9 s** (a second independent run on a more loaded machine gave 132–229 s).

There is no contradiction between "18 s in production" and "105–229 s locally." They are the
same function at opposite ends of its input range. Duration tracks solar-eligible area, and
production has only seen the cheap end.

Two risks converge tonight:

- **Timeout.** 300 s ceiling, `MaximumRetryAttempts: 0`, no DLQ. A timed-out scan vanishes silently.
- **Memory.** 707 MB observed at 22% eligibility, against 3008 MB. `_swath_cell_is_land`
  (`opportunity_ledger.py:237`) carries a module-level `@lru_cache(maxsize=750_000)` that survives
  warm invocations — 304,994 entries after one local scan, measured at 199 B/entry
  (60.7 MB after one scan, 149.2 MB at capacity).

> **Discard the "16x headroom" reading of the production logs.** It measures the wrong regime.

### Seasonal note

The `docs/opportunity-ledger-proof-results.md` claim that the 18:46Z shadow run proves production
safety (4.46 s / 180 MB) is a **null-workload** measurement — 0.0% of CONUS was eligible. It is
also a *seasonal* best case: at 2026-01-15 18:46Z, **99.8%** of CONUS is eligible. Across a full
day, 197 of 288 five-minute slots have some envelope and 79 exceed 60%. This is routine load.

---

## 2. Attribution — what the changeset introduced

**HEAD has no research-review lane at all.** `git grep research_possible HEAD` returns exactly one
hit: a dormant allow-list string in `worker/review_callback.py:19` that nothing at HEAD produces.
`git log --all --diff-filter=A` confirms the new worker modules never existed in history.

Consequence: **both** the ledger lane and the geometry-first detector-research lane
(`worker/research_review.py`, dispatched from `shadow_lambda.py:62-63`) are new in this tree.
There is no prior research lane to fall back to. The public detector is separate and untouched.

| Defect | Attribution |
|---|---|
| 35 km research match radius (`go-event-common.mjs:551-554`) | INTRODUCED |
| `ZADD` into `GO_EVENT_INDEX` (`go-event-common.mjs:581`) | INTRODUCED |
| Double research cap (`go-events.mjs:52-53`, `:145-146`) | INTRODUCED |
| Missing-`distanceKm` bypass (`go-events.mjs:139`) | INTRODUCED |
| `confirmedGalleryRecord` lost review-only gate (`:394-410`) | INTRODUCED |
| Unguarded catalog load (`shadow_lambda.py:62-63`) | INTRODUCED |
| Decision-log mutation + metric ordering (`research_review.py:115-118`) | INTRODUCED |
| `matchingEvent` lacks `candidateType` filter (`:269`) | PRE-EXISTING, **latent → live** |
| `mergeDetection` flips to `live_go` (`:216-218`) | PRE-EXISTING, **latent → live** |
| Raw GET leaks `researchAssessments` (`go-events.mjs:238`) | PRE-EXISTING, verbatim |
| No CAS on go-event writes | PRE-EXISTING, aggravated + 1 new writer |
| `SET NX` without rollback (`:561` vs `:583`) | PRE-EXISTING, aggravated |

The single `ZADD` at `go-event-common.mjs:581` is what made the two latent defects reachable.
HEAD's `attachReviewAssessments` wrote `SET` only, never indexed, and never created events.

---

## 3. Blocking findings

### B1 — Full-CONUS build has no timeout margin
`worker/opportunity_ledger.py:355-358`, `template.yaml:220`

Measured 105–229 s for `build_opportunity_ledger` alone; the handler then adds S3 fetch,
`load_previous`, a 6 MB gzip + PUT, camera selection over 960 sites, GOES/METAR I/O, and an
HTTPS callback. cProfile attributes **80–86%** to `is_conus_land`
(`worker/detector_core.py:51-63`), a pure-Python ray-cast: 96.7 s tottime over 4.7 M
`_inside_ring` calls.

**This is also a correctness bug.** `load_previous` resolves the previous ledger by listing S3
and taking the newest key earlier than the current scan. At 105–229 s against a 300 s cadence the
duty cycle is 35–76%. If run N starts before run N−1 has written, run N **silently links lineage
to a two-scans-old ledger** — wrong `eventId` continuity, wrong `lineageEdges`, no error. The
slow build corrupts the identity chain the persistence gate depends on.

**Fix (five lines, no new artifact).** `shapely==2.1.1` is already in `worker/requirements.txt:8`,
already imported by `worker/map_tiers.py:7`, and `worker/conus-land.json` (140 KB) is already in
the image and already loaded by `detector_core.py:23`. Replace the per-cell loop with
`shapely.contains_xy` over arrays that are already numpy:

```python
from shapely import contains_xy, prepare
from shapely.geometry import shape

@lru_cache(maxsize=1)
def _conus_geometry():
    payload = json.loads(Path(__file__).with_name("conus-land.json").read_text(encoding="utf-8"))
    geometry = shape(payload["geometry"])
    prepare(geometry)
    return geometry
```

then at `:350-358`:

```python
keep = contains_xy(_conus_geometry(),
                   CONUS_BOUNDS[0] + columns * SWATH_RESOLUTION_DEG,
                   CONUS_BOUNDS[1] + rows * SWATH_RESOLUTION_DEG)
cells = set(zip(rows[keep].tolist(), columns[keep].tolist()))
```

| variant | total | land test | speedup |
|---|---|---|---|
| current ray-cast | 109.0 s | 88.4 s | 1.0x |
| **shapely `contains_xy`** | **18.6 s** | 0.92 s | **5.9x** |
| precomputed bitset | 14.3 s | 0.19 s | 7.6x |

Equivalence proven, not argued: the full ledger SHA-256 on a real full-CONUS scan (437,274
projected cells) is byte-identical across ray-cast, shapely, and mask —
`e39ee422425ce62d75a03489edc4d5c7fa99149c21da4d0f63bf19e54443205a`.

**A precomputed land-mask bitset was considered and rejected.** It buys 4% more runtime in
exchange for a binary artifact that must stay in lockstep with `conus-land.json`. It would also
need to be **1041 × 2361**, not 1040 × 2360 — the clip at `:346-349` is inclusive and the
divisions land on exactly 1040.0/2360.0, so the smaller dims would silently negative-index-wrap
the lat=50.0 row and lon=−66.0 column. And it cannot be built at image-build time from
`is_conus_land` (24 minutes).

Also **delete `_swath_cell_is_land` and its `lru_cache`** — it memoizes a function whose complete
answer set is 300 KB packed, never converges, and retains up to 149 MB permanently.

Note `is_conus_land` must stay: `detector_core.py:183` still calls it on arbitrary off-grid offsets.

### B2 — Lineage joins physically unrelated storms
`worker/opportunity_ledger.py:97-101`

`_component_gap` measures **bounding-box** Chebyshev separation, not cell distance. Any new
component whose bbox falls inside a large storm's bbox scores gap 0 — the strongest fallback —
regardless of true distance. Two reviewers reproduced independently: a fresh 1-cell shower ~330 km
from any rain inherited a crescent storm's `eventId` with `overlapCells: 0, boundingBoxGapCells: 0`.
Routine for bow echoes and frontal lines.

**Fix:** compute the gap from actual cells — dilate the smaller cell set by `maximum_motion_cells`
and intersect to stay O(n) — and reject fallbacks exceeding it.

### B3 — A one-cell speck can rename a large tracked event
`worker/opportunity_ledger.py:168-175`

Sort key is `(-overlapCells, boundingBoxGapCells, match["eventId"])`. The final tiebreak is
lexicographic on a hash. Reproduced: 1-cell and 99,800-cell parents tied at 1 overlap cell; the
speck won. This is verbatim what `docs/opportunity-ledger-v1.md:31-33` forbids. The fallback path
at `:157` slices `[:1]` off a `set`, so it is arbitrary too.

**Fix:** insert `-match["cellCount"]` before the `eventId` term; sort the fallback by
`(gap, -cellCount)`. Add a regression test with tied `overlapCells` — the existing test at
`test_opportunity_ledger.py:58-67` only covers unequal overlap and passes today.

### B4 — "Two-scan persistence" has no temporal bound
`worker/opportunity_ledger_store.py:62-73`, `worker/ledger_review.py:82`, `:137`

`load_previous` lists yesterday and today and returns `earlier[-1]` with **no age check**.
Reproduced across a 45-minute gap: identity inherited, candidate selected, `persistenceScans`
reported as 2. Worst case ~48 h. Compounding it, `"persistenceScans": 2` at `:137` is a hardcoded
literal — never a measurement — and the gate at `:82` is only `eventId in previous_events`.

**Fix:** pass `scan_time` into `load_previous`; reject a previous ledger older than ~12–15 min for
the **selection** gate (keep full lineage for history); emit the real scan-time delta.

### B5 — Sunlight-v2 is visible before grading, three ways
`api/go-events.mjs:238`, `:258`, `:217`

1. `:238` — plain `GET /api/go-events` returns **raw event objects** including
   `researchAssessments[].sunlightState`.
2. `:258` — POST returns the whole event after grading one view, while sibling camera views of the
   same event are the next queue items.
3. `:217` — `reviewResults` attaches `sunlightAssessment` for any event with ≥1 graded view, and
   that table stays on screen.

`authorized()` at `:23-24` is `hasReviewSession(req) || verifySecret(req, body)`, so a plain review
cookie suffices. `HttpOnly` is no protection — the reviewer navigates to
`/api/go-events?limit=250` and the browser attaches it. **Reachable by exactly the person it must
be hidden from.** No test covers this invariant.

**Fix:** project the GET response to drop `researchAssessments`; on POST, reveal only when *every*
group in `evidenceFrameReviewGroups(event)` is non-pending; same gate in `reviewResults`.

### B6 — Ledger assessments bind to unrelated live GO events at 35 km
`api/go-event-common.mjs:551-554`

Widens `radiusKm` to 35 (vs the 3 km default at `:472`) but passes the **unfiltered** event list to
`matchingAssessmentEvent`. The sole caller (`api/review-assessment.mjs:33`) passes no options, so
`options.researchRadiusKm` is always undefined and 35 km is unconditional.

**Fix:** try the strict radius first; retry the widened radius only against
`events.filter(e => e.candidateType === "research_possible")`, or match on `ledgerEventId` identity
when present.

### B7 — Live GO detections get absorbed into ledger events
`api/go-event-common.mjs:581` + `:247-252` + `:269` + `:216-218`

The new `ZADD` puts `research_possible` events into `GO_EVENT_INDEX`. `loadRecentGoEvents` applies
no type filter, `saveGoEvents:269` calls `matchingEvent` without one (40 km / 30 min defaults at
`:25-33`), and `mergeDetection` rewrites the event to `candidateClass:"GO", candidateType:"live_go"`.

A real GO merges into a private ledger record, inherits its `id`, `firstSeenAt`, `researchSource`,
`ledgerEventId`, and escapes the research gates. If that ledger item was already graded
`no_rainbow`, `reviewQueue`'s first filter drops it — **a genuine GO event is never reviewed**.

Public output is safe: `notify-alerts.mjs:179` derives alerts from `alertableGoCandidates(artifact)`,
not from the event store.

**Fix:** `matchingEvent(recent.filter(e => e.candidateType !== "research_possible"), detection)`;
make `mergeDetection` refuse to downgrade a research `candidateType`.

### B8 — No compare-and-swap on any go-event write
`saveGoEvents:264-280`, `attachReviewAssessments:543-583`, `attachGoEventEvidence:459-467`,
`labelGoEvent:292-322`, `labelGoEventView:354-389`

Every writer does GET → mutate whole object → SET. `redisPipeline` is a pipeline, not `MULTI`. The
changeset turns a rare race into a 5-minute-cadence one: two 300 s fire-and-forget Lambdas now write
through `attachReviewAssessments` into the same keys, and per-view grading multiplies POSTs.

Confirmed loss: the ledger callback reads event E, FAA capture writes fresh frames 100 ms later,
the callback SETs its stale copy and wipes `event.evidence`. `reviewQueue` filters on
`frames.length > 0`, so the item silently vanishes. Same shape reverts a human grade to `pending`
after `syncConfirmedGallery` already published a gallery record.

**Recommended fix: byte-CAS via `EVAL`**, guarding on the SHA-1 of the exact bytes read. No
`version` field, no key splitting, **no data migration** — storage format stays byte-identical and
legacy events work on first write.

This is safe because every writer touches a **disjoint field set** (verified): core detection state,
`evidence`, `review`, `viewReviews[key]`, `researchAssessments` respectively. Nothing needs conflict
*resolution* — only detection plus re-application, which always converges.

Two designs to reject explicitly:

- **`WATCH`/`MULTI` is unavailable.** Upstash REST is stateless; classic optimistic locking is out.
- **A Lua-side merge reintroduces the bug it fixes.** Redis `cjson` encodes an empty table as `{}`
  not `[]`, so `evidence.frames: []` becomes an object, `(frames || []).length > 0` goes false, and
  review items silently vanish. Merge in JavaScript; use Lua only for the atomic compare.

Fold `ZADD GO_EVENT_INDEX` into the same script — today blob and index can silently diverge.
Ship behind a `GO_EVENT_CAS` env kill switch (the writers are on a cron you cannot pause).

### B9 — Unguarded research calls abort the sunlight-v2 lane
`worker/shadow_lambda.py:62-63`

`load_faa_catalog()` and `select_research_candidates()` are unwrapped and run **before** the S3
write at `:73` and `safe_push_review_assessments` at `:80`. A malformed catalog entry or unexpected
record shape discards the entire sunlight-v2 enrichment for that scan and skips the callback — for
*all* events, not just research. This breaks an established convention: HEAD ships seven `safe_*`
wrappers around every other step in this pipeline. Same shape at `opportunity_ledger_lambda.py:55-60`.

### B10 — In-place mutation of the frozen decision log
`worker/research_review.py:115-118`, `worker/shadow_lambda.py:62-64`

`record["disposition"] = "selected_research_possible"` overwrites the original verdict, and
`shadow_lambda.py` writes the envelope back to the **same S3 key** it read. Worse,
`disagreement_metrics(records)` runs at `:64` *after* the mutation, so the v1/v2 disagreement rate
is bucketed by the overwritten disposition in the same run.

**Fix:** preserve `record["originalDisposition"]` before overwriting; compute metrics first.

### B11 — Double cap silently erases a candidate
`api/go-events.mjs:52-53` and `:145-146`

`reviewQueue` caps two research **events**; `reviewQueueItems` caps two research **items**. If the
first event has two camera groups it consumes both item slots and a second distinct candidate —
already admitted by the event cap — yields zero items with no operator signal. Contradicts the
design doc's "cannot silently erase it."

Note the strict max-two invariant itself **holds**. The bug is over-enforcement, not under.

**Fix:** delete the event-level cap; the item-level cap is already correct and post-expansion.
`review-workflow.test.mjs:262-266` currently *asserts the buggy behavior* and must be rewritten.

---

## 4. Non-blocking, worth fixing

- **`redisPipeline` swallows per-command errors** (`api/alert-common.mjs:124-137`) — checks only
  top-level `data.error`, but Upstash reports pipeline failures per element. A failed `ZADD` leaves
  the event written but unreachable, with no exception.
- **Gallery items never expire** (`go-event-common.mjs:440`) — `SET` with no `EX`.
- **The 40 km camera boundary is dead code** — `ledger_review.py:101` sets the observer to the
  camera site's own coordinates, so `distanceKm` is always 0.0. The real constraint is ±1-cell
  swath containment (~2.8 km). `thresholdSnapshot.maximumCameraDistanceKm: 40` is misleading.
- **`fov = finite(...) or 45.0`** (`research_review.py:57`) — falsy-zero bug; and
  `float(camera["bearing"])` raises instead of skipping a camera with unknown orientation.
- **Detector lane uses the 80 km default** (`research_review.py:105`), above the doc's 40 km rule.
- **Two `selectionReason` strings** for one concept (`ledger_review.py:134` vs
  `go-event-common.mjs:516`).
- **Bow-top height computed two ways** on one page (apparent vs geometric elevation).
- **`rainPoint.bearing` fabricated** as the anti-solar bearing rather than the true observer→rain
  azimuth (`go-event-common.mjs:515`).
- **No DLQ / `OnFailure` destination** and no `ReservedConcurrentExecutions`.
- **Changeset hygiene:** the 511GA Georgia camera source (`api/dot-camera-common.mjs`),
  `api/dot-evidence.mjs`, and the `index.html` solar-band change are **unrelated work** riding
  along in the same deployed tree. A blanket `git checkout HEAD -- .` would revert deployed
  public-map behavior. Rollback must be surgical.

---

## 5. Rollback

**Smallest safe first action:** unset `RAINBOW_REVIEW_ENRICH_URL` on `OpportunityLedgerWorker` only.
One `aws lambda update-function-configuration`. No Vercel deploy, no SAM deploy, no rebuild, no
touching the working tree. `push_review_assessments` returns `{"ok": True, "skipped": True}` cleanly
(`review_callback.py:164-171`). Effective next cycle.

Stops every new ledger-derived Review candidate at the source. Leaves untouched: public GO/POSSIBLE
detection, ZIP cards, emails, existing camera reviews, in-flight pending items, recorded grades, and
the sunlight-v2 shadow lane.

**Given tonight's timing, pair it with unsetting `RAINBOW_OPPORTUNITY_LEDGER_FUNCTION` on
`RainbowWorker`** — a total ledger kill including storage. You lose the research record for the
disabled window permanently (14-day lifecycle), but you remove the timeout and memory exposure
rather than betting on an untested regime.

### On keeping storage active — a correction

An earlier analysis claimed storage survives a timeout because `persist()` precedes the enrichment.
**That is backwards.** The handler order at `opportunity_ledger_lambda.py:28-61` is:
fetch → `load_previous` → **`build_opportunity_ledger`** → `persist` (line 50) → select → enrich →
callback. The expensive step runs *before* the write. On a stormy scan you can lose the entire
ledger object. Storage-on is safe in blast radius but **lossy exactly when the data matters**.

### What the rollback does NOT stop

1. **The geometry-first lane keeps running** (`shadow_lambda.py:62-63`), 6-per-scan cap vs the
   ledger's 2, **no kill switch at all**, identical absorption defect.
2. **The root defects are untouched** — B7 and B8 remain.
3. **Redis residue persists** — existing ledger events keep occupying both review slots and one of
   two FAA capture slots per run, until graded or until the 90-day TTL. The absorption hazard from
   residue self-expires after 30 min (the `matchingEvent` gap), but queue pollution does not.
4. **`sam deploy` silently restores it** from `template.yaml:230`. Track as known drift.

**Do not** lower `GO_EVENT_RETENTION_DAYS` to expire residue — it is global and would truncate the
TTL on live GO events and recorded grades.

### Corruption audit (read-only)

Live GO events may already be corrupted via B7. To detect:

`ZREVRANGEBYSCORE rainbow:go:events +inf <deployTimeMs>` → `MGET` → flag any event with
`candidateType === "live_go"` that also carries `researchSource`, `ledgerEventId`,
`researchRuleVersion`, or non-empty `researchAssessments`.

Unambiguous proof of absorption: a single event whose `detections[]` contains **both** a synthetic
research detection (`evidence.selectionReason` ending `review-only`) **and** a real detector
detection (`evidence.goes` or `nearestZip` present).

**Repair, never delete** — strip the research fields, drop synthetic detections, recompute
`firstSeenAt`/`scanCount`/`detectionCount`, preserve `review`/`viewReviews` verbatim.

---

## 6. Production settings

| Setting | Current | Recommended | Rationale |
|---|---|---|---|
| Timeout | 300 s | **120 s** | At 300 the timeout *equals* the cadence, so a run that hits it is guaranteed to overlap the next dispatch — which triggers the `load_previous` lineage corruption in B1. Also `api/goes_sample.py:157` uses `timeout=60` as a *per-read* timeout with `stream=True`, so a slow-but-alive GOES connection has no total bound. |
| MemorySize | 3008 | **hold 3008 for now** | See disagreement D3 below. |
| EphemeralStorage | 2048 | **keep 2048** | `Band2Sampler` caches per satellite position, not per record: worst case ~600 MB. `SunlightShadowWorker`'s 3072 is correctly larger; the asymmetry is right. |
| Concurrency | unset | `ReservedConcurrentExecutions: 2` | Guarantees capacity, bounds a runaway. |
| Cadence | 5 min | **keep 5 min** | Post-fix p99 of 60 s is a 20% duty cycle. Do **not** shard — `rain_components` builds 8-connected components from row runs, so a storm crossing a shard boundary splits into different `componentId`s and lineage won't link. Incremental is worse: lineage already provides the temporal link. |

Runtime target post-fix: **p50 10 s, p99 60 s**, allowing ~1.3x for Graviton2 on interpreted Python.

**Alarms — there are currently none, account-wide.** Minimum set: `Duration` (Maximum > 90 s, 2 of 3
periods), `Duration` p99 > 60 s hourly, `Errors` > 0 (2 of 3), `Throttles` > 0,
`AsyncEventsDropped` > 0, and a staleness alarm. The staleness signal has no native metric — emit one
EMF line after `persist(...)` with `LedgerObjectWritten` and `GenerationLagSeconds`, then alarm with
`TreatMissingData: breaching` (silence *is* the failure). A metric-math fallback on
`Invocations − Errors` catches "not running" but **not** "ran and silently wrote nothing."

---

## 7. Acceptance tests

Each must **fail against the current tree**:

1. **No GO absorption** — seed a `research_possible` event; submit a live GO detection 20 km away
   within 30 min; assert a *new* event is created and the research event's `candidateType` is unchanged.
2. **No cross-storm binding** — a ledger assessment with `ledgerEventId` X must not attach to an
   event lacking X at any distance, nor beyond ~10 km without one.
3. **Concurrency** — interleave callback, camera capture, and a human grade against a fake Redis;
   assert all three fields survive.
4. **Blinding** — for a 3-group event, grade one; assert `researchAssessments` is absent from the
   POST response, from `reviewResults`, and from the raw GET, until all three are non-pending.
5. **Cap** — two research events × three groups yields exactly two items **spanning both events**.
6. **Bounded persistence** — a previous ledger older than the adjacency bound yields zero selected,
   and emitted `persistenceScans` equals measured lineage depth.
7. **Lineage determinism** — tied `overlapCells` resolves to larger `cellCount`; two components with
   overlapping bboxes but cells 300 km apart produce no link.
8. **Python→JS contract** — a golden JSON fixture emitted by the Python selector and consumed by
   `go-event-common.mjs`. **Today, renaming `source` → `lane` in Python silently degrades every
   ledger candidate to `detector_rejection_log` while all 90 JS and 81 Python tests still pass.**

Also: **commit the replay fixtures.** `.gitignore:22` ignores `validation/` wholesale, so
`case-fixture-v1.json` and `replay-report-v1.json` — the proof-gate evidence — are untracked and
`test_opportunity_replay.py` fails on a fresh clone. `git check-ignore` exits 0 on them.

---

## 8. Patch sequence

**Immediate (before 23:30Z tonight)**
1. Commit this tree to a branch — production is currently unreproducible.
2. Unset `RAINBOW_REVIEW_ENRICH_URL` on the ledger worker; also unset
   `RAINBOW_OPPORTUNITY_LEDGER_FUNCTION` on `RainbowWorker`.
3. Add Duration + Errors alarms.

**Correctness blockers** — B7, B6, B5, B11, B2, B3, B4, B9, B10.

**Performance blockers** — the shapely change (B1), delete the `lru_cache`, timeout → 120 s, then
re-benchmark at 23:30Z-class load and read `Max Memory Used` before touching `MemorySize`.

**Test / CI** — the eight acceptance tests, committed fixtures, and CI (`.github/workflows/` is absent).

**Non-blocking** — everything in §4.

---

## 9. Open disagreements for Codex to adjudicate

**D1 — Should storage stay on during the disable window?**
My position: no, given the corrected handler ordering. A stormy scan can lose the whole object
anyway while billing the full timeout, and the lineage-staleness bug in B1 means a partial history
is worse than none. Counterargument: research coverage for the window is permanently lost (14-day
lifecycle) and the blast radius genuinely is nil. **I hold this loosely.**

**D2 — Is the shapely fix safe to land before re-enabling, or after?**
Output equivalence is proven byte-identical by SHA-256, and it needs no new artifact — which argues
for landing it early, since it also fixes the lineage-staleness correctness bug. But it touches the
hot path of a live system with no CI. **Recommend: land it, but only after the lane is disabled, so
a regression cannot reach the Review workbench.**

**D3 — MemorySize 1769 vs holding 3008.**
The analysis for 1769 is sound in principle — everything on the critical path is single-threaded
(CPython, GEOS `contains_xy`, numpy *elementwise* ufuncs; no BLAS anywhere), so 3008 is a 1.7x
overpay for zero speed, worth ~$172/yr. **I disagree on sequencing.** Production showed 707 MB at
only 22% eligibility; removing the `lru_cache` reclaims at most ~149 MB of that; and no heap profile
completed. Cutting headroom before ever measuring the 94% regime trades $14/month against an OOM in
the one regime never tested. Hold 3008 through the first post-fix stormy night, read the `REPORT`
line, then right-size.

**D4 — Is the geometry-first lane also unsafe?**
It shares B7, B8, B9, B10 and B11, has a 6-per-scan cap, and has **no kill switch**. Disabling it
requires a code change and a container redeploy to `SunlightShadowWorker`. I did not recommend it in
the immediate step because it needs a deploy, but if the absorption defect is judged severe enough to
stop the ledger lane, the same logic applies here. **Needs a call.**

**D5 — Claims I could not fully verify.**
Runtime evidence covers only ~2h10m since deploy, within a single UTC day. The day-boundary rollover
in `load_previous` (which reads `observed − 1 day` and `observed` prefixes) is **unobserved, not
cleared**. Sustained multi-day payload growth likewise. And I could not probe the live Upstash
instance — the token in `.env.local` returns `WRONGPASS` (a known stale-secret issue in this repo).
**Before relying on the CAS design, run one probe:** `["EVAL","return redis.sha1hex('x')",0]` must
return `11f6ad8ec52a2984abaafd7c3b516503785c2072`. If `redis.sha1hex` is absent, pass the read blob
as `ARGV[2]` and compare directly (same semantics, ~2x upload). If `EVAL` is refused entirely, fall
back to a `SET NX` lock and accept the weaker guarantee.

---

## 10. Go / no-go for re-enabling

Do not re-enable until **all** are true:

- [ ] A 23:30Z-class stormy scan benchmarked at **p99 ≤ 60 s against a 120 s timeout**, and checked
      against a winter-equivalent solar profile (18:46Z is 99.8% eligible in January)
- [ ] `Max Memory Used` measured on a stormy invocation; `MemorySize` right-sized against it
- [ ] Duration, Errors, Throttles, AsyncEventsDropped, and staleness alarms live
- [ ] Research and live-GO matching provably isolated (test 1)
- [ ] Ledger assessments cannot bind to unrelated events (test 2)
- [ ] Sunlight-v2 blinded **server-side** until all sibling views graded (test 4)
- [ ] One post-expansion cap that cannot starve (test 5)
- [ ] Persistence temporally bounded and honestly reported (test 6)
- [ ] Lineage deterministic under ties and distance-bounded (test 7)
- [ ] Python→JS contract pinned by a golden fixture (test 8)
- [ ] Replay fixtures committed; the six frozen cases re-run green
- [ ] CAS deployed behind its kill switch, with the `redis.sha1hex` probe passed
- [ ] The tree committed, so production is reproducible

---

## 11. Verification of the repair commits — 2026-08-01

Scope: the 32 commits `master..HEAD` (`1115004` → `7392f34`) on `safety/ledger-review-repair`.
Method: three independent reviewers (API side, worker side, tests/contracts/commit-sweep), each
executing the original counterexamples against the repaired code; key claims re-verified against
source before inclusion. Production state read directly from AWS the same morning.

### 11.1 Current production state (verified 2026-08-01 ~09:20 CDT)

| | |
|---|---|
| Deployed image | `v20260731-callback-logging-v1` — the repair branch IS deployed (07-31 12:20Z) |
| Review callback | **OFF** — `RAINBOW_REVIEW_ENRICH_URL` absent from the ledger worker |
| Ledger storage lane | ON — dispatch env var set, one object per 5 min, current |
| Timeout / memory | 120 s / 3008 MB |
| Alarms | 11 exist across all four workers, all OK |
| Timeouts/errors since Jul 30 | zero |
| Stormy-window durations | Jul 30 (old code, peak eligibility): max 32.7 s. Jul 31 (repaired): max **56.9 s** vs 120 s (~2.1x margin), max memory 1280 MB of 3008 |

The 1280 MB stormy reading settles §9-D3: holding 3008 MB was correct (1769 would have left 1.4x).
Production has exercised the Lua CAS (`EVAL`) on every scan for two days — the §9-D5 probe is
empirically satisfied, though there is still no kill switch or fallback.

### 11.2 Verdict table

| Finding | Verdict |
|---|---|
| B1 land test / timeout | **FIXED** — vectorized `contains_xy`, 0/8000 mismatches vs old ray-cast, `lru_cache` deleted. Margin evidence partial: recorded benchmark (22bb037, 17.92 s) measured a null-lineage, null-selection run; real stormy production shows 56.9 s / 120 s |
| B2 bbox lineage joins | **FIXED** — exact-cell gap; original counterexample rejects (gap 13, true distance 295 cells); clean 12-cell cutoff sweep |
| B3 speck renames storm | **FIXED** — `-cellCount` tiebreak on both paths, executed 1 vs 99,800 both ways; tie regression test exists for the overlap path only |
| B4 unbounded persistence | **FIXED** — `MAX_PREVIOUS_AGE_MINUTES = 15` enforced fail-closed in store AND selection gate; `persistenceScans` measured (executed 1→5), `scanGapMinutes` recorded. Residual: a 10-min-old ledger (one skipped scan) still counts, and `scanGapMinutes` is not surfaced to the reviewer |
| B5 sunlight blinding | **FIXED** — server-side via `reviewSafeEvent` on GET, POST, and results; strong tests incl. stranded-event carve-out. Residual: `selectionReason` / `currentDetectorDisposition` / `researchSource` / `ledgerEventId` still leak on raw GET/POST while blinded (the anchoring fields `queueItem` deliberately withholds) |
| B6 cross-storm binding | **PARTIALLY FIXED — see N1, the remaining blocker** |
| B7 GO absorption | **FIXED** — three layers (pool prefilter, `matchingEvent:258` skip, CAS-internal recheck closing the TOCTOU); reverse direction closed; tested. Residual: archived `archive-faa-*` events are still absorbable to `live_go` (binary research/non-research guard) |
| B8 no CAS | **FIXED** — genuine server-side sha1 compare-and-swap (`EVENT_CAS_LUA`), no Lua-side merge (cjson hazard avoided), ALL five writers routed through it, idempotency claim moved after the write, `redisPipeline` per-command errors surfaced. Residuals: no probe/kill-switch/fallback; ZADD still outside the script so blob/index can diverge |
| B9 unguarded calls | **FIXED in `shadow_lambda.py:87-97`** (executed: catalog failure → enrichment, S3 write, and callback all still run, failure surfaced). **NOT fixed in `opportunity_ledger_lambda.py:59-67`** — harm bounded because `persist()` now runs first; only that scan's callback is lost |
| B10 decision-log mutation | **FIXED** — `originalDisposition` preserved and transmitted; metrics computed pre-mutation (executed). Residual: `eligibility()` ignores `originalDisposition`, so a re-enrichment after a METHOD_VERSION bump would drop previously-selected candidates |
| B11 double cap | **FIXED** — single post-expansion two-pass cap; executed: 2 items across 2 events, 2 views within 1 event; cannot starve or exceed |

Suites: **115/115 JS, 109/109 Python** (pytest from repo root; the documented
`cd worker && python -m unittest discover` is broken — `test_zip_location_ingestion.py` needs the
repo-root path shim every other worker test has). Acceptance tests: **7 of 8 strong** and would
catch their original defect; test 2 passes for the wrong reason (see N1). Contract fixture pinned
(`scripts/fixtures/review-callback-contract-v1.json`) — renaming `source` in Python now goes red,
though in a Python test rather than the JS side. Validation fixtures committed (143 files tracked),
but `.gitignore:22` still swallows `validation/`, so new artifacts need `git add -f`; and the six
frozen cases are **pinned by hash, not re-run** — nothing feeds the 22 tracked ledger JSONs through
`build_report`. CI: still absent.

### 11.3 N1 — cross-storm binding (HIGH) — **FIXED in `5e92b25`, verified 2026-08-01**

> **Verification (Claude, independent, against Codex's implementation):** all four review
> counterexamples re-executed against `5e92b25` via a fake-Redis harness — **all pass**.
> - **CE1** (production trigger — one batch, two new ledger ids, storms 19 km apart): pre-fix
>   `{attached:2, created:1}` with one merged event; now **two events**, each with its own
>   `ledgerEventId` and exactly one assessment.
> - **CE2** (identity overwrite — assessment `rain-X` vs stored event `rain-Y` at 19 km): Y
>   untouched, identity preserved, new event created for X.
> - **CE3** (legitimate persistence — same id, next scan): still attaches, no duplicate,
>   `scanCount` increments. No regression.
> - **CE4** (exact-point replay of a ledger candidate): the new guard means the 0.25 km
>   replay-target validation can never pass for ledger-identified assessments, but the replay
>   **still lands on its named target via the identity match** (a stronger check than location) —
>   no duplicate. Exact-point replay remains valid; re-check this path when the replay scripts
>   are fixed.
> - Suites after the fix: **116/116 JS, 109/109 Python** (repo root, `.venv`). A locally
>   reported 106 was an environment artifact (missing `xarray` collects 3 fewer files).
> - Residuals (non-blocking): the identity match still has no distance bound (~590 km case —
>   tolerable now that B2 makes shared ids mean genuine lineage); the new storage test's
>   explicit `ledgerEventId === "ledger-Y"` assertion was added post-verification.

The original finding, for the record:

`api/go-event-common.mjs:658-664` + `:700`. When a research assessment's `ledgerEventId` matches no
stored event, the `||=` chain falls through to a **35 km radius match against other research
events**, and the mutator then executes
`current.ledgerEventId = assessment.researchReview.ledgerEventId` **unconditionally** — overwriting
the bound event's identity with the wrong storm's id.

Independently reproduced by two reviewers. The production trigger needs no bad luck:
`MAX_PER_SCAN = 2`, so a first callback batch carrying two candidates from storms <35 km apart
mints **one** merged event — executed proof: `{received: 2, attached: 2, created: 1}`, one event at
storm X's observer point labeled with storm Y's `ledgerEventId`, holding both assessments. The
grader sees storm Y's sunlight verdict on storm X's candidate, defeating the blinding work.
Subsequent assessments then ping-pong the identity. The `ledgerEventId` identity match also has
**no distance bound** (±12 min only — verified binding at ~590 km along one lineage id).

**Fix (small):** when `research && ledgerEventId` finds no event, **create** a new event instead of
falling through to the radius match; and only assign `current.ledgerEventId` when it is currently
unset. **Must change together:** `scripts/go-events.test.mjs:85-92` currently asserts the 35 km
corridor behavior as desired (the fix will look like a regression), and acceptance test 2
(`event-store-safety.test.mjs:91`) needs a real negative case — assessment carrying id X vs stored
event carrying id Y at ~19 km.

### 11.4 Other new defects from the repairs (selected, by severity)

**Replay/backfill scripts — park until fixed (do not run):**
- `scripts/replay-review-sunlight-v2.py:245-250` — `--redeliver` writes to production WITHOUT
  `--apply` (returns before the gate).
- Both scripts hard-exit at 1000 stored events; `/api/go-events` has no time filter or cursor, so
  `--start/--end` cannot slice the window. Store was at 869 and grows up to ~288/day — imminent.
- The `sunlightAssessment is None` filter confuses "missing" with "withheld by blinding"
  (`sunlightAssessmentWithheld` exists but is not read) → replays double-attach, and the replayed
  verdict becomes `.at(-1)`, i.e. what the grader sees.
- `:156-158` — an exact-point replay fabricates a rain point 15 km along the anti-solar bearing
  with no marker in the record.
- Test coverage of all these paths: zero.

**Pipeline robustness:**
- `saveGoEvents` (`go-event-common.mjs:336-369`) — one bad event aborts the whole scan's
  persistence, surfaced only as a `console.warn` in notify-alerts; and the scanKey rollback makes a
  retried scan double-count `detectionCount`/`detections[]` (executed). Fix: per-candidate
  try/catch + dedupe by `detectedAt` (as `attachReviewAssessments` already does).
- Research events can consume ALL capture slots in 4 of 5 camera lanes — only FAA is gated
  (`dot-evidence.mjs`, `alertca-common.mjs`, `usgs-nims-common.mjs`, `webcoos-common.mjs` have no
  `research_possible` filter and no `scanCount >= 2` gate).
- `template.yaml` Errors alarms use `EvaluationPeriods: 2 / DatapointsToAlarm: 2` — with
  `MaximumRetryAttempts: 0`, a single lost scan produces one breaching datapoint and never alarms.
  Fix: 1/1 for Errors (keep 2/2 for Duration).

**Worker/lineage:**
- The exact-cell gap (B2's fix) is superlinear pure-Python: ~25 s at 2,566 components
  uncorrelated, ~42 s at 4,000. Fits under 120 s today (production stormy max 56.9 s), but the
  margin is ~2.1x, not the 6.7x the null-regime benchmark implies. Cheap fix: bbox gap as an
  O(1) upper-bound prefilter before the exact computation (bbox gap ≤ true gap, so it is sound);
  early-out on gap 0.
- Split siblings share one `eventId`, so the next scan's primary-parent tiebreak
  (`match["eventId"]`) is non-total — `persistenceScans` swung 8 vs 3 on identical geometry
  (executed). Fix: final tiebreak on `componentId` (globally unique) in both sorts.
- `boundingBoxGapCells` in persisted `lineageEdges` now carries the exact cell gap — a field whose
  name asserts the defect that was removed. Drop or rename.
- Empty FAA catalog: hard-fails the ledger lambda (`opportunity_ledger_lambda.py:60-61`) but is
  caught-and-surfaced in `shadow_lambda.py` — make the lanes agree deliberately.

**Public-surface hygiene (index.html / structural):**
- Public isolation is structural, not type-keyed: `mapPossibleCandidates` (`index.html:1262`)
  promotes every non-`go` artifact entry to a public pin with no `candidateType` check, and
  `publicArtifact` (`api/candidates.mjs:50`) strips private buckets by deny-list. Safe today only
  because nothing writes research candidates into the artifact. Fix: positive allow-list, pinned
  by a test.
- The new stale-readout banner can't fire on its own failure path (`loadCandidateMap` swallows its
  errors; `lastRefreshOk` set on partially-failed cycles).

### 11.5 Updated go/no-go for re-enabling the callback

Ticked since §10 was written: stormy benchmark with margin (56.9 s / 120 s, production), alarms
exist, blinding server-side, single cap, bounded persistence, lineage determinism (except the
split-sibling tiebreak), GO/research isolation, CAS deployed and empirically exercised, contract
fixture, fixtures committed, tree committed and pushed.

**Not ticked:**
- [x] **N1 fixed** (`5e92b25`) + corridor test rewritten + real acceptance test 2 — verified
      via CE1–CE4, see §11.3
- [ ] Errors alarms at 1/1
- [ ] Per-candidate error isolation + detections dedupe in `saveGoEvents`
- [ ] Research filters in the four ungated camera lanes
- [ ] Replay scripts fixed or formally parked
- [ ] Six frozen cases actually re-run (not just hash-pinned)
- [ ] CI (must run pytest from repo root, not the broken worker-local unittest command)

Items 1–4 are each small, well-localized changes. N1 is the only one that blocks correctness of
the Review lane itself; the rest are robustness and observability.

### 11.6 Callback re-enabled — 2026-08-01

Every gate above was completed and independently verified (N1 `5e92b25`, isolation `dc2e652`,
camera gates through `930c1dc`, replay safety `07879ae`, frozen reruns `b68a4de`, CI green on a
real runner at `f8565c1`, Vercel production deployed from the branch tip). With explicit
authorization, `RAINBOW_REVIEW_ENRICH_URL` was restored to the ledger worker via `template.yaml`
(commit `1d84f88`) and deployed by CloudFormation with all stack parameters reused. Post-deploy:
env present with the correct URL, timeout/memory/image unchanged, zero alarms firing, first
cycles at 26–34 s / ≤691 MB, and the ingress path confirmed live via the shadow worker's
delivery logs. The geometry-first research lane remains disabled
(`RAINBOW_RESEARCH_REVIEW_ENABLED=false`), and the template invariant test now pins the
re-enabled state.

Residual observability item: the per-scan `[review-callback]` delivery-summary print exists only
in `shadow_lambda.py` — port the same line to `opportunity_ledger_lambda.py` so a healthy ledger
lane is visible in its own logs, and so the first real candidate delivery can be affirmatively
confirmed rather than inferred.

---

## Appendix — verification method

Findings came from five independent parallel reviews (isolation/publication, selection geometry and
cap, review-workflow display, identity/merging and callback semantics, ops/cost/tests), followed by
five follow-up investigations (change attribution, live deployment verification, rollback design,
Redis concurrency design, land-mask measurement). **Every blocking finding was then re-verified
directly against the source** before inclusion here; several subagent claims were corrected in the
process, including the "storage survives timeout" claim in §5 and the "16x headroom" reading in §1.

Test status at time of review: `node --test` 90/90 pass; `python -m unittest discover` in `worker/`
81/81 pass (requires `.venv`; system Python 3.14 lacks `xarray`, which also makes
`worker/test_review_callback.py` uncollectable there). **All green — the suites do not cover any of
the blocking findings above.**
