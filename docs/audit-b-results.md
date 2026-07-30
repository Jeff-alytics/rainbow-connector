# Audit B: confirmed-positive recall baseline

Audit B freezes the known positive evidence before any production detector thresholds change. It is a recall dataset, not a precision dataset: it intentionally contains no negative controls and therefore cannot estimate false-positive rate.

## Frozen inventory

- 19 positive records total.
- 13 camera-confirmed events inside the contiguous-U.S. production scope.
- 2 camera-confirmed science-only events in Alaska and Hawaii.
- 4 social reports: Baltimore, Dundalk, Person County, and Idaho.
- 3 social reports have point references with explicit location uncertainty. Idaho remains region-only because no city was established.
- Baltimore and Dundalk are separate observer reports but one meteorological event group.
- Release A live-scan baseline: 1 GO, 38 POSSIBLE, no worker errors, 15.625-second runtime at 2026-07-29T14:20:25.435629Z.

The normalized fixture is content-hashed. Regeneration from the source review files is deterministic, and every derived report records the fixture hash it used.

## Results

### Solar-elevation policy

All 16 point-scorable contiguous-U.S. positive reports had some portion of their observation window within:

- the physical -0.833 to 42 degree bow window;
- the worker's current 0 to 30 degree seed window; and
- the current strict-GO 5 to 22 degree band.

No known positive in this fixture exists only in the 30 to 42 degree band. The audit provides no positive-evidence reason to widen the production worker above 30 degrees yet. This is a small, selection-biased sample and does not prove that higher-sun bows do not occur.

### GOES gate

Historical ACMC and DSRF pixels were sampled at the review reference time for all 13 camera-confirmed contiguous-U.S. events, using the operational historical satellite, including GOES-16 for the 2024 ARM event.

| Satellite result | Confirmed events |
| --- | ---: |
| ACMC clear | 2 |
| ACMC cloudy | 9 |
| ACMC missing pixel | 1 |
| ACMC unavailable | 1 |
| DSRF at least 200 W/m2 | 1 |
| DSRF below 200 W/m2 | 10 |
| DSRF missing pixel | 2 |
| Both ACMC clear and DSRF at least 200 W/m2 | 1 |
| Does not pass the satellite pair | 12 |

A single-point, single-time ACMC plus fixed-DSRF agreement is not a valid required gate for rainbow recall. Broken-cloud sun showers commonly leave the observer's cloud-mask pixel classified cloudy. DSRF is downward shortwave flux rather than direct-normal irradiance, and its own product metadata limits good-quality quantitative retrievals to solar zenith angles of 0-70 degrees. Values beyond that range are degraded or invalid, covering much of the low-sun period when bows are most visible.

Inside the product's quantitative range, the detector experiment should test DSRF normalized by clear-sky expected DSRF at the event's solar elevation and location. It should retain DQF and evaluate temporal and spatial neighborhoods rather than using one pixel as a veto. Outside the quantitative range, both DSRF and its clearness ratio are unavailable; normalization cannot turn an invalid retrieval into sunlight evidence.

Most archive labels identify a five-frame window rather than the exact positive frame. That timing uncertainty can hurt a single-time satellite comparison. It does not rescue a gate that passed only 1 of 13 positives.

### Social-report recall

The Baltimore/Dundalk bow time is now based on a firsthand report: about 23:30-23:34 UTC, with a conservative audit window of 23:25-23:45 UTC. The small retained production-artifact sample contains no scans in that corrected window, so the earlier broad-window nearest-candidate distances are not valid evidence for the actual bow time and have been withdrawn. Archived radar replay, rather than retained public artifacts, is the evidence used below.

Person County and Idaho remain region/window recall probes. They are useful for asking whether a future rule covers the reported event, but their uncertain observer locations and times are not threshold-fitting data.

### Baltimore miss diagnosis

An archived MRMS replay at the corrected time shows that radar generation found the event geometry:

- At 23:30 UTC, production stride 10 generated an observer seed 6.0 km from Baltimore. Its rain target was 25 km away to the east, its radar score was 88.2, and the sun was 8.6 degrees high.
- At 23:40 UTC, the nearest seed was 7.0 km from Baltimore, with rain 15 km away, radar score 79.8, and sun elevation 6.8 degrees.
- At 23:30 UTC, GOES ACMC labeled both the city reference points and the nearby detector seed cloudy. DSRF was 2.7 W/m2 at Baltimore and 40.1 W/m2 at the seed.
- At 23:40 UTC, ACMC was still cloudy. DSRF was 15.3 W/m2 at Baltimore and 43.1 W/m2 at the seed.
- The sun was only about 8.5 degrees high, placing these DSRF samples outside the product's 0-70 degree quantitative solar-zenith range. The raw values therefore mean unavailable, not dark. The 23:30 seed also failed both narrow geometry-fallback limits: radar score below 90 and rain distance above 15 km. At 23:40, distance reached the limit but radar score remained below 90.

Classification: corrected-time radar generation is confirmed, but the exact production disposition cannot be reconstructed because no public artifact scan from that window was retained and rejected-seed logging did not yet exist. The strongest supported diagnosis is that radar found a credible backside-of-storm bow setup, while the existing sunlight/fallback pathways would reject the replayed seeds. The exact Open-Meteo DNI at those rejected seeds is unknown.

### Person County miss diagnosis

Person County does not support fitting the same rescue threshold to a second event:

- At 23:20 UTC, production stride 10 generated a radar score 99.2 seed 54.3 km from the report reference point. It survived nationwide clustering at rank 21 of 347.
- Another seed 59.3 km away scored 96.4 with modeled rain 25 km away.
- As the nearest seeds moved toward the report point later in the evening, their radar scores fell: 85.4 at 23:40, 76.0 at 23:50, and 67.9 at 00:00 UTC.
- By 00:10 UTC the nearest seed was only 3.6 km from the reference point, but the sun was about 2 degrees high and the radar score was 54.2.

Classification: radar generation, clustering, and the 400-candidate cap did not erase the early strong geometry. The social post does not establish the image capture minute or a precise observer point, so this fixture cannot determine whether the bow corresponds to the early strong seed or the later weak nearby seeds. Treat it as timing/location-limited evidence, not as support for a particular fallback threshold.

### Fallback threshold workload

A nationwide raw-MRMS replay covered all 144 ten-minute scans on July 28. This was necessary because the published artifact history does not retain radar seeds rejected during sunlight enrichment. It compared the legacy fallback (score at least 90, sun 5 to 22 degrees, rain within 15 km) with distance-broadened candidates allowing rain within 40 km.

| Radar threshold | Added scan detections | Added 40-km/30-min event clusters | Scans where added seeds could fill all 6 review slots |
| ---: | ---: | ---: | ---: |
| 88 | 922 | 338 | 25.7% |
| 90 | 607 | 243 | 20.1% |
| 92 | 500 | 216 | 16.7% |
| 95 | 343 | 167 | 11.8% |

These are conservative radar-only upper bounds: sunlight enrichment would reduce the number actually published as fallback POSSIBLEs. They nevertheless show that a nationwide `score >= 95, distance <= 40 km` rule is not selective enough by itself. It could crowd the review queue and would create far more new events than the current human-review capacity.

The ten-minute replay undersamples production's five-minute cadence and the corrected event peaks at only 88.2 in this replay, so it should not be used to select a knife-edge threshold. The correct next test is the negative precision fixture plus explicit per-seed rejection logging at production cadence. No fallback threshold or distance gate changes from this audit.

### Rain distance

Exact worker rain distance exists for only two confirmed positives, and both were 8 km. Historical camera records establish rain but do not preserve production MRMS geometry. This is not enough evidence to fit a distance falloff or tighten the current 35-km camera search rule.

## Precision twin

The frozen negative fixture now contains 47 human-graded `no_rainbow` events from the review store. It retains every grade for transparency, but only 26 multi-frame, timely, known-bearing FAA reviews currently meet the version-1 calibration bar. Nineteen remain weak nonzero evidence and two unknown-bearing reviews have zero negative weight. The fixture is sanitized, deterministic, and content-hashed at `validation/audit-b/negative-precision-fixture-v1.json`.

No production gate change should ship from the positive audit alone. Score positives and these weighted negatives with the same features:

- DSRF quality flag and quantitative solar-zenith validity;
- quality-valid point DSRF and clear-sky-normalized ratio;
- quality-valid temporal and spatial neighborhood ratios;
- ACMC neighborhood clear fraction;
- solar elevation, rain distance, and radar score.

The side-by-side report must show both positive recall and the added false-GO/POSSIBLE rate for every proposed threshold.

## Decisions supported now

1. Keep the 0 to 30 degree worker seed window until a positive case probes 30 to 42 degrees.
2. Do not use ACMC plus fixed DSRF agreement as a hard positive requirement or hard negative veto.
3. Proceed with the per-scan MRMS rain-footprint sidecar.
4. Use the frozen, graded-negative precision fixture before any sunlight gate change ships; weak views cannot materially penalize a rule.
5. Test quality-gated, clear-sky-normalized DSRF with temporal and spatial neighborhoods; treat low-sun degraded retrievals as unavailable.
6. Do not tune rain-distance weighting yet; preserve geometry for every future confirmed positive.
7. Do not broaden the strong-geometry fallback nationally using radar score alone; the measured review-volume cost is too high even at 95.

## Next implementation sequence

1. Phase 0: emit one compact run-length-encoded rain-footprint sidecar per MRMS scan, with candidates linking to the scan and rate tiers.
2. Precision twin maintenance: keep the frozen v1 fixture unchanged and version any later rebuild that uses Phase 1 visible-bow fractions.
3. Phase 1: timestamp validation plus camera-perspective bow-arc sampling, producing visible-bow and rain-backed-bow fractions without an assumed rain-depth rejection gate.
4. Detector experiment: compare the current fixed DSRF behavior with DQF/bounds-gated, clear-sky-normalized, temporal, and spatial features on both fixtures.
5. Only after the side-by-side regression should production GOES, distance, sun-band, or clustering rules change.
