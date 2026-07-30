# Opportunity Ledger v1 proof results

## Outcome

The private, camera-independent ledger retained all 10 confirmed observation
locations across all six frozen cases:

- Baltimore and Dundalk
- Silver West, Saguache, and Fremont County, Colorado
- Ogden and Bear River, Utah
- Middletown, Connecticut
- Meeker, Colorado
- Delano low-sun case

Middletown uses a deliberately broad 30-minute interval ending at the exact
tweet-post time. It is a recall probe, not an asserted capture timestamp.

The content-hashed machine report is
validation/opportunity-ledger/replay-report-v1.json.

## Production-scale benchmark

Method: observer-swath-physical-envelope-2026-07-v3-viable-only.

A full-CONUS scan from 2026-07-30 01:30Z produced:

- 2,399 rain events and opportunities
- 337,607 observer-swath cells
- 11,147 derived representative candidates
- about 0.88 MB compressed

A pre-repair two-scan full-CONUS test (01:30Z and 01:40Z), including 2,951
lineage links on the second scan, completed in 140 seconds locally. That result
did not provide adequate Lambda timeout margin and is retained here only as a
historical baseline.

After replacing the per-cell Python land test with vectorized Shapely geometry,
the 2026-07-29 23:32Z scan completed locally in 23.56 seconds. This deliberately
high-load scan put about 93.8% of CONUS inside the solar-eligibility envelope
and produced:

- 2,566 rain events
- 1,897 viable bow opportunities
- 522,641 observer-swath cells
- 17,742 derived representative candidates

The opportunity Lambda timeout is now 120 seconds, with alarms at 90 seconds
and on any Lambda error. Final safety evidence requires the same scan to run in
the deployed arm64 Lambda; the local number alone is not the release gate.

## Safety boundary

The production design runs as a separate asynchronous private worker after
candidate publication and subscriber notification. Failure cannot block pins,
ZIP cards, alerts, or email. Email remains a one-way GO notification channel
and is never used to request or record confirmation.

Rolling opportunity ledgers expire after 14 days. Frozen regression fixtures
and reports remain separate.

## Production shadow verification

The v3 worker entered production shadow mode on 2026-07-30. Its first verified
object covered the 18:46Z radar scan and completed in 4.46 seconds using 180 MB
of its 3,008 MB allocation. That scan had no viable observer swaths, so this is
explicitly a null-workload smoke test and is not performance evidence. The
working scan above is the relevant local benchmark pending an arm64 production
shadow replay.

The decompressed SHA-256 exactly matched the private S3 metadata. The object
also carried the source rain-footprint hash, v3 method version, AES256 storage
encryption, scan/generation timestamps, and the 14-day expiration rule.
