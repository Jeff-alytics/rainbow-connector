# Candidate decision log v1

## Purpose

The private candidate decision log makes every detector decision replayable. It records the complete evidence vector for every radar-shortlisted observer seed, whether the seed becomes GO, becomes POSSIBLE, or is rejected.

This is not part of the public candidate artifact. It must not affect the public map, ZIP cards, GO classification, or email alerts.

## Storage and retention

- One gzip-compressed JSON object per detector scan.
- Rolling objects live under `decision-log/rolling/YYYY/MM/DD/`.
- Rolling retention is 14 days, enforced by an object-store lifecycle rule.
- A confirmed camera rainbow, credible social report, or explicit research annotation freezes the intersecting time/location window under `decision-log/frozen/` with no automatic expiration.
- Freezing copies only the relevant scan objects and records why they were retained.
- Logs contain no subscriber email addresses, ZIP subscriptions, authentication tokens, or camera image bytes.

At 250 to 400 records per five-minute scan, this design avoids putting high-volume research data in Redis or the public Vercel artifact.

### Proposed AWS implementation

- A dedicated private S3 bucket created by `template.yaml` with Block Public Access enabled and default server-side encryption.
- The worker role receives only `PutObject` for rolling decision logs and sidecars. A separate maintenance path performs freezes/copies and expiry audits.
- Object keys include schema version and scan timestamp so schema generations cannot collide.
- The rolling-prefix lifecycle expires objects after 14 days. No expiry rule applies to the frozen prefix.
- Writes are gzip-compressed and include a SHA-256 of the uncompressed JSON in object metadata.
- A failed log write is reported in worker health and CloudWatch, but candidate publication and alerts continue.

This storage plan must be approved before the bucket, IAM permissions, or Lambda write path are deployed.

## Envelope

```json
{
  "schemaVersion": "candidate-decision-log.v1",
  "detectorRuleVersion": "noaa-detector-2026-07-v1",
  "scanId": "mrms-20260728T230200Z",
  "generatedAt": "2026-07-28T23:04:31Z",
  "radar": {
    "provider": "NOAA MRMS PrecipRate",
    "observedAt": "2026-07-28T23:02:00Z",
    "sourceKey": "CONUS/PrecipRate_00.00/...",
    "rainFootprintId": "mrms-footprint-20260728T230200Z"
  },
  "retention": {
    "class": "rolling",
    "expiresAfterDays": 14,
    "freezeEligible": true
  },
  "records": []
}
```

## Candidate record

Each record contains:

```json
{
  "candidateId": "stable hash of scan + observer + rain point",
  "disposition": "selected_go | selected_possible | rejected",
  "decisionStage": "model_prefilter | satellite_corroboration | final_selection",
  "decisionReasons": ["model_dni_below_watch"],
  "features": {
    "observer": { "lat": 39.3256, "lon": -76.7651 },
    "rain": {
      "lat": 39.295,
      "lon": -76.595,
      "distanceKm": 15,
      "rateMmHr": 3.1,
      "observerRateMmHr": 0.0,
      "antiSolarRainArcSpanDeg": null,
      "antiSolarRainOccupiedDeg": null,
      "antiSolarRainSegmentCount": null
    },
    "geometry": {
      "sunElevationDeg": 14.38,
      "sunBearingDeg": 283.1,
      "antiSolarBearingDeg": 103.1,
      "radarScore": 94.4
    },
    "model": {
      "directNormalIrradianceWm2": 0,
      "cloudCoverPct": 100,
      "observedAt": "2026-07-28T23:00:00Z",
      "error": null
    },
    "satellite": {
      "acmc": { "value": 1, "observedAt": "2026-07-28T23:01:00Z", "fresh": true },
      "dsrf": { "valueWm2": 5.2, "observedAt": "2026-07-28T23:00:00Z", "fresh": true, "dqf": 1, "solarZenithDeg": 81.5, "quantitativeSolarZenithBoundsDeg": [0, 70], "usable": false, "unavailableReason": "outside_quantitative_solar_zenith_range" },
      "clearSkyExpectedDsrfWm2": null,
      "dsrfClearnessRatio": null,
      "clearSkyMethodVersion": null
    },
    "persistence": { "priorMatchingScans": 0 },
    "thresholdSnapshot": {
      "directWatchMinWm2": 120,
      "directGoMinWm2": 200,
      "geometryFallbackRadarMin": 90,
      "geometryFallbackRainDistanceMaxKm": 15,
      "geometryFallbackObserverRainMaxMmHr": 0.05,
      "optimalSunMinDeg": 5,
      "optimalSunMaxDeg": 22
    },
    "sunlightV2": null
  }
}
```

Raw DSRF, DQF, product quantitative solar-zenith bounds, location, timestamp, and solar elevation must always be retained when available. The clear-sky expected value and ratio may be computed only for a good-quality pixel inside the quantitative bounds; otherwise they remain null with an explicit reason. This keeps degraded low-sun retrievals from becoming false negative evidence while preserving the raw value for audit.

## Decision reason vocabulary

- `selected_strict_go`
- `selected_model_possible`
- `selected_geometry_fallback_v1`
- `model_dni_below_watch`
- `model_uniform_overcast`
- `model_data_unavailable`
- `geometry_fallback_radar_below_min`
- `geometry_fallback_rain_too_distant`
- `geometry_fallback_sun_outside_band`
- `geometry_fallback_observer_not_dry`
- `satellite_blocked`
- `satellite_unknown`
- `satellite_sampling_error`
- `final_policy_rejected`

Multiple reasons may apply. The rule version and threshold snapshot prevent old and new fallback outcomes from being mixed.

Reason codes describe why every available selection pathway passed or failed. A model-prefilter rejection may therefore contain both a model reason and one or more geometry-fallback failure reasons. A fallback-prefixed reason does not mean the fallback was attempted as a separate processing stage; it means that criterion prevented the fallback pathway from rescuing the candidate.

## Precision-fixture rule

A human `no_rainbow` label is not automatically a full negative. Its negative-evidence weight is computed separately from:

- verified frame timestamps and coverage of the predicted window;
- known camera bearing and bow overlap;
- usable sky fraction;
- image freshness and quality;
- number of distinct frames.

Unknown camera bearing forces negative-evidence weight to zero. Weak negatives may remain in the dataset for transparency but cannot materially penalize a detector rule.

## Non-negotiable behavior guard

Decision logging is observational. A failure to write the private log must be visible in worker health but must never block publication of an otherwise healthy candidate artifact or prevent a GO email.
