# FAA retrospective validation — v1 plan

## Feasibility result

The FAA WeatherCam API is live, but the public image endpoint did not return a retrospective archive for the pilot window tested:

- `2026-07-01T00:00:00Z`–`2026-07-02T00:00:00Z`: 0 images at 3 Alberta sites and 0 images at 3 Florida sites.
- `2026-08-01T00:00:00Z`–`2026-08-02T00:00:00Z`: 564 usable images at 3 Florida sites.

The conclusion is limited to the public endpoint: it has current data, but no demonstrated pre-review retention. Do not treat an empty historical response as a negative weather label.

## Probe

The read-only probe is `scripts/probe-faa-archive.mjs`. It performs only FAA API `GET` requests and writes a metadata manifest only when `--output` is supplied. It does not download images, call the review store, or write production data.

Example:

```powershell
node scripts/probe-faa-archive.mjs `
  --start 2026-07-01T00:00:00Z `
  --end 2026-07-02T00:00:00Z `
  --states FL `
  --sample-sites 3 `
  --output C:\tmp\faa-probe.json
```

Each manifest pins the catalog SHA-256, requested window, probe schema, and the sunlight method labels used by the planned comparison: production V1 and `sunlight-v2-shadow-2026-07-v3`.

## Next implementation

Use an archived source with actual historical retention (WebCOOS/ARM) for the retrospective V1/V2 study. Keep the study data outside the live review event store. Reuse the review UI's blinding conventions through a study-specific manifest and batch namespace, but do not call `saveHistoricalReviewEvent`.

FAA remains useful for a prospective collection: retain image bytes and metadata at capture time, then review after the collection window is frozen. That prospective dataset must be kept separate from the retrospective holdout.

The full study should still use the agreed controls: deterministic manifests, score-band stratification after observing the score distribution, event-level holdout excluding tuned frozen cases, season and local-time strata, a second reviewer or delayed rereview, and archived radar if persistence is evaluated.
