# Negative precision fixture v1

## Purpose

This frozen fixture contains human-reviewed `no_rainbow` events for precision comparisons. A review is evidence about what a camera captured, not automatic proof that no rainbow existed. Every record remains visible, while weak views receive little or zero negative-evidence weight.

## Calibration eligibility

Version 1 requires all of the following:

- known camera bearing with predicted-bow difference no greater than 30 degrees;
- camera distance no greater than 35 km;
- at least two distinct frames;
- a nearest frame within eight minutes of the candidate time;
- an adequate sky view, established by reviewed view quality, measured sky fraction, or the established FAA horizon-camera source;
- multiplicative negative-evidence weight of at least 0.5.

Unknown bearing always produces zero negative weight. Limited or unreviewed views, single frames, stale frames, and poorly aligned views remain in the fixture but cannot materially penalize an experimental detector.

The v1 weight combines direction, proximity, timing, frame coverage, and sky-view components. Camera-bearing difference is temporarily a proxy for actual bow visibility. Phase 1 will replace that proxy with camera-perspective visible-bow and rain-backed-bow fractions, then version and rebuild the fixture rather than silently changing v1.

## Privacy and retention

The fixture contains event IDs, timestamps, coarse detector/camera evidence, weights, and reason codes. It deliberately omits image URLs, review-session credentials, subscriber data, and personal information. Its canonical content hash makes later changes detectable.
