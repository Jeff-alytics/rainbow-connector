# ARM Lamont feasibility results

Status: feasible for retrospective validation; positive rainbow labels are not
yet established.

## What was verified

- `sgpasiskyimageC1.a1` provides 1536 x 1536 full-sky JPEGs at 15-second
  cadence in daily archives.
- The camera field of view and image quality are sufficient for manual rainbow
  review.
- IEM NEXRAD N0Q mosaics provide a useful independent precipitation screen at
  five-minute resolution.
- Rain must be screened in the anti-solar sector, not merely at the camera.
- Range matters: echoes more than roughly 22 km away often do not occupy enough
  of the visible sky to be useful.
- Open-Meteo hourly precipitation and DNI are useful only for coarse candidate
  discovery. They are not observation truth and must not be used as labels.

## Reviewed events

| Pair | UTC time | Camera/radar result | Rainbow |
| --- | --- | --- | --- |
| lamont-001 | 2025-06-14 00:00 | Clear camera view despite modeled heavy rain/cloud; weak radar echo | No |
| lamont-017 | 2024-05-26 00:00 | Widespread nearby rain, overcast sky, wet lens, no direct sun | No |
| lamont-004 | 2026-06-26 23:00 | Nearby partial shower and broken clouds; no visible bow in +/-10 min sequence | No |
| lamont-019 | 2024-04-06 23:00 | Direct sun visible; radar echoes only beyond 22 km | No |
| lamont-007 | 2025-08-03 23:00 | Small echo about 10 km anti-solar; sun intermittently blocked | No |

The absence of a positive in this small first pass is expected: rainbows require
a narrow spatial and temporal overlap of direct sunlight, droplets, observer
position, and viewing geometry.

## Reproduce

Generate the coarse manifest:

```powershell
node scripts\arm-lamont-feasibility.mjs
```

Rank its events using archived radar:

```powershell
C:\Users\jeffm\rainbow-goes-prototype\.venv\Scripts\python.exe scripts\arm-radar-prefilter.py
```

Download only one event and extract a 20-minute sequence at 30-second cadence:

```powershell
node scripts\arm-lamont-feasibility.mjs --use-manifest --download --pair 7 --event-only --window-minutes 10 --step-seconds 30
```

Daily tar archives and extracted frames are intentionally ignored by Git.

## Next scientific improvement

Scan radar at five-minute cadence across the camera interval, then detect direct
sun visibility from the camera frames themselves. Download and label only times
where a nearby anti-solar echo and an unobscured solar disk overlap. A larger
candidate pool should be generated before estimating sensitivity, precision, or
false-positive rate.
