# FAA retrospective validation — v1 plan

## Feasibility result

The FAA WeatherCam API exposes a rolling public archive. The probe confirmed:

- `2026-07-01T00:00:00Z`–`2026-07-02T00:00:00Z`: 0 images at 3 Alberta sites and 0 images at 3 Florida sites; this was outside the suspected 30-day retention period.
- `2026-07-10T00:00:00Z`–`2026-07-11T00:00:00Z`: 584 usable images at 3 Florida sites.
- `2026-08-01T00:00:00Z`–`2026-08-02T00:00:00Z`: 564 usable images at 3 Florida sites.

The practical constraint is retention: FAA is suitable for a retrospective pilot only if collection and review happen inside the rolling window. Do not treat an empty response outside that window as a negative weather label.

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

FAA can be the first retrospective source, but the study must be planned, frozen, and reviewed inside the rolling retention window. WebCOOS/ARM remain useful when a longer historical window is needed. Keep the study data outside the live review event store. Reuse the review UI's blinding conventions through a study-specific manifest and batch namespace, but do not call `saveHistoricalReviewEvent`.

FAA also remains useful for a prospective collection: retain image bytes and metadata at capture time, then review after the collection window is frozen. That prospective dataset must be kept separate from the retrospective holdout.

The full study should still use the agreed controls: deterministic manifests, score-band stratification after observing the score distribution, event-level holdout excluding tuned frozen cases, season and local-time strata, a second reviewer or delayed rereview, and archived radar if persistence is evaluated.
