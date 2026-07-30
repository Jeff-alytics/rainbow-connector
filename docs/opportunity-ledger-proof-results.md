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

A two-scan full-CONUS test (01:30Z and 01:40Z), including 2,951 lineage links
on the second scan, completed in 140 seconds locally. This is about 70 seconds
per scan and below the five-minute production cadence.

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
of its 3,008 MB allocation. It retained 2,526 rain events and correctly emitted
zero bow opportunities because the viable observer swaths were empty at that
high-sun instant.

The decompressed SHA-256 exactly matched the private S3 metadata. The object
also carried the source rain-footprint hash, v3 method version, AES256 storage
encryption, scan/generation timestamps, and the 14-day expiration rule.
