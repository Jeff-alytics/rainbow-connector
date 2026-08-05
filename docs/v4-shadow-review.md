# V4.2 shadow review lane

V4 is the selector and ranker for the private, post-publication research lane. It does not change the operational detector, public GO/POSSIBLE classifications, alerts, subscribers, or notification credentials.

## Prospective contract

- Source: the complete camera-independent detector decision pool retained in each MRMS rain-footprint sidecar.
- Storm mechanics: frozen `mrms-hysteresis-object-lineage-2026-08-v1` segmentation and ancestry.
- Score: frozen `causal-bounded-persistence-radar-v2` mechanics. Human labels are never scorer inputs.
- Eligibility: at least 3 lineage scans, causal peak radar at least 95, and current radar at least 70.
- Retention lane: every mechanically eligible ancestry family per scan; there is no rank cap.
- Decision lane: causal sunlight evidence classifies each retained family as `GO_SUNLIT_SUPPORTED`, `POSSIBLE_PLAUSIBLE`, `POSSIBLE_UNRESOLVED`, `POSSIBLE_UNAVAILABLE`, or `WITHHELD_OVERCAST`.
- Projection lane: viable families are sent to Review only on a classification change or their first dispatch of the UTC day. Failed or disabled projections remain in the S3-carried backlog and retry later; same-family scans replace rather than multiply a pending daily row. This controls Upstash writes without deleting candidates. Affirmative overcast records stay in S3 but are withheld from review.
- Split handling: reduce split siblings to the best family representative before applying eligibility, then rank by score, causal peak radar, persistence, and event ID.

Every mechanical prediction is serialized and SHA-256 hashed before camera matching, imagery retrieval, sunlight-v2 enrichment, or human review. After enrichment, a second frozen hash covers the causal evidence cutoff, sunlight state, and final classification. S3 retains every evaluated record and is authoritative; the Review store is a rebuildable projection.

## Review page

`review.html` has an authenticated **V4 Shadow** queue filter and a **V4.2 candidates by day** table. A shadow GO is a private research prediction, never a public Connector GO. The daily table retains GO and POSSIBLE predictions even when the system has no matching catalog camera and provides:

- UTC date/time
- map-linked observer coordinates
- predicted bow bearing
- rank, pool size, and V4 score (withheld while matched imagery is awaiting a grade)
- catalog-camera and evidence-frame status
- review status
- frozen prediction ID/hash and model version

Repeated scan selections of the same storm family are collapsed to one row per UTC day, with a scan-selection count, so the camera-search list remains practical. The ordinary review card remains blinded: detailed selection context and model assessment are not exposed until all camera views are graded. Existing human grade values and operational review queues remain unchanged.

## Storage and kill switch

The asynchronous Opportunity Ledger worker runs only after live publication. It persists `v4ShadowState` and `v4ShadowPrediction` in the private rolling ledger so ancestry and frozen predictions survive between scans. `RAINBOW_REVIEW_ENRICH_URL` remains the callback kill switch: when absent, V4 state and predictions continue to be stored privately, but nothing is sent to the Review site.
