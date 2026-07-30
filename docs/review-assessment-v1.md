# Review assessment callback v1

## Purpose

Make causal sunlight-v2 and rain-shape evidence available beside human camera grades without changing detection, public map states, ZIP cards, or email alerts.

## Contract

- Endpoint: `POST /api/review-assessment`
- Authentication: dedicated bearer secret; never the alert or artifact-publish secret
- AWS source: the separate sunlight shadow Lambda
- Secret storage: AWS Systems Manager SecureString `/rainbow-connector/review-enrich-secret`; Vercel `REVIEW_ENRICH_SECRET`
- Envelope schema: `review-assessment.v1`
- Maximum body: 128 KiB and 32 assessments; larger scans are split into independently idempotent batches below both caps. Receivers reject rather than silently trim an oversized batch.
- Idempotency: SHA-256 of private candidate ID, radar timestamp, detector rule version, and sunlight method version; stored with Redis `SET NX`
- Scope: selected GO, selected POSSIBLE, and future budget-selected research POSSIBLE dispositions only

The private detector candidate ID is not the public candidate ID. The receiver matches an assessment to a Review event using radar time and observer coordinates, then retains the private ID solely for replay and idempotency.

## Reproducibility fields

Each assessment includes detector and sunlight method versions, threshold snapshot, observer/rain/geometry features, assessment latency, four-state sunlight result and reasons, Band-2 file keys with scan and creation times, ACMC and DSRF keys, METAR station IDs and observation times, and rain-footprint ID/content hash.

## Review behavior

The reviewer first labels whether direct sunlight is visible and grades the rainbow imagery. Only after saving does Review reveal v2's state and compact supporting details. The results table keeps the human sunlight label beside v2 so agreement can be measured during the shadow period.

## Failure behavior

Oversized, unauthorized, malformed, duplicate, or unmatched assessments cannot affect the detector. Callback errors are returned in the shadow Lambda result and never block or alter public candidate publication or subscriber notification.
