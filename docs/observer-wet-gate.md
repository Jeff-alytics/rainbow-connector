# The observer-wet gate suppresses real rainbows

A confirmed false negative, 2026-07-31 at Rocky Mountain Metro Airport (FAA site
569). Photographed by two cameras across four frames while the detector
published nothing at that location.

## What the detector rejected

`worker/detector_core.py:16`

```python
OBSERVER_DRY_MAX_MM_HR = 0.05
```

Any observer position with more than 0.05 mm/h falling on it is discarded before
any other test. Measured MRMS PrecipRate at the field:

| Scan | Precip at the field | Verdict |
| --- | --- | --- |
| 00:34Z | **1.30 mm/h** | rejected |
| 00:40Z | **2.80 mm/h** | rejected |

Every other condition was satisfied, at both scans:

| | 00:34Z | 00:40Z |
| --- | --- | --- |
| sun elevation | 17.84° | 16.70° |
| sun azimuth | 279.3° | 280.2° |
| antisolar bearing | 99.3° | 100.2° |
| bow envelope `-0.833 ≤ elev < 42` | inside | inside |
| rain on the antisolar ray, 5–40 km | 27.5, 30, 32.5, 35, 37.5, 40 km | 5, 7.5, 10, 37.5, 40 km |
| peak on that ray | 4.90 mm/h | 2.80 mm/h |
| wet samples in a ±25° wedge | 75 | 93 |

Two consecutive scans six minutes apart, which clears the two-scan persistence
bar. The FAA cameras recorded the bow at **00:33Z and 00:41Z** (camera 12076,
bearing 138, SE) and **00:35Z and 00:43Z** (camera 12075, bearing 54, NE).

## Why the assumption is wrong

Rain on the observer does not prevent a rainbow. Sun low in the west with rain
falling locally and downrange to the east is one of the most ordinary ways a
rainbow is seen at all. At 1.3 mm/h the beam plainly reached the scene — the
photographs are the proof.

The instinct behind the gate is defensible: heavy rain overhead usually means
cloud thick enough to extinguish the direct beam. But that is a statement about
**sunlight**, and the pipeline already measures sunlight directly — Open-Meteo
DNI plus GOES DSRF/ACMC corroboration, which is what produced
`"goes": {"decision": "go", "positiveSources": ["goesDsrf", "goesAcmc"]}` on the
event the system *did* publish that afternoon.

The gate proxies a question the system can already answer. It should ask the
question instead.

## Suggested change

Replace the hard reject with a sunlight test at the observer. Rain on the
observer becomes an input to the sunlight decision, not a veto. If the direct
beam is corroborated at the observer, wetness there is irrelevant to whether a
bow is visible.

Before believing the size of this, measure it: re-run several days of archived
scans with the gate relaxed and count how many additional GOs appear and how
many survive review. The gate has been rejecting silently, so the current
false-negative rate is unknown rather than small.

## What the event that *was* published looked like

Published 00:25:00Z, expired 00:32:00Z — one minute before the first camera
frame showing the bow, and 37.9 km west of the field.

```
observer   39.7245, -105.4852     bearing 98°     sun elevation 20.15°
rainPoint  39.695, -105.195       25 km @ 98°
verdict    go / high              persistence scanCount 1, confirmed false
```

It is not the same phenomenon. From Rocky Mountain Metro that rain cell sits at
bearing 195.9°, 98° away from the field's own antisolar point, so it cannot
produce the photographed bow.

## Two further defects found on the way

**The camera matcher ignores each site's own geometry.** `matchFaaCameras` in
`api/go-evidence-common.mjs` computes one bow arc at the modelled observer, then
scores every camera within 80 km on bearing overlap and distance-to-observer. It
never asks whether the camera's own site satisfies the bow condition. For this
event:

| Site | Distance to the modelled rain | In the 5–40 km band? | Rain vs its own antisolar |
| --- | --- | --- | --- |
| Berthoud Pass | 49.9 km | **no** | 5° |
| Dakota Hill | 36.2 km | yes | 24° |
| Rocky Mountain Metro | 25.0 km | yes | 98° |

Berthoud Pass is beyond the worker's own `MAX_DISTANCE_KM = 40.0`, so no bow
from that cell is visible there at all — and it received **two of the three**
evidence slots. The cheap fix is to apply the same geometry filter the worker
already uses before scoring candidate cameras.

**The opposite-leg rule missed by three degrees.** After picking the top camera
the matcher looks for a second at least 45° away. Rocky Mountain Metro's SE
camera is `|138 − 95| = 43°` from the top pick, so nothing qualified and the
slot fell through to a second Berthoud Pass camera. The site whose two cameras
actually bracket the arc — NE covering 60.3–76.5°, SE covering 115.5–135.7°, 48%
of the bow between them — placed 5th of 8 against a 3-slot limit.

Scoring cameras individually cannot express "this site catches both ends".
Joint arc coverage per site would.

## Why this case is worth keeping

Two independent cameras, four frames, ten minutes, with radar and solar geometry
independently confirming. That makes it a usable regression fixture for the gate
change and a natural historical case for the event-grouping work in
`docs/rainbow-events-v1.md`.

Fixture: `scripts/fixtures/rocky-mountain-metro-2026-07-31.json`, generated from
the archived MRMS scans rather than transcribed.
