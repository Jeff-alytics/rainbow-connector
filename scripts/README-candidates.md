# Rainbow Candidate Backend

`build-candidates.mjs` is the first backend-style detector for GO-only map pins.
It runs the nationwide scan once, writes a small `candidates.json`, and lets the
static frontend fetch that artifact instead of recomputing the map in every
visitor's browser.

## Run locally

```powershell
node scripts/build-candidates.mjs
```

Useful options:

```powershell
node scripts/build-candidates.mjs --dry-run
node scripts/build-candidates.mjs --max-verify 300
node scripts/build-candidates.mjs --output candidates.json
```

## Data flow

1. Fetch the latest RainViewer radar frame metadata.
2. Fetch the CONUS radar tiles once at zoom 6.
3. Scan land grid points for valid sun geometry.
4. Sample radar in a widened anti-solar fan, 5-48 km from each observer point.
5. Cluster the strongest geometry+radar candidates.
6. Batch-check clustered observer points with Open-Meteo
   `current=cloud_cover,direct_normal_irradiance_instant`.
7. Treat observer sunlight as present when either Open-Meteo cloud or direct
   radiation says the observer is sunlit. If one of the two passes and the other
   fails, publish the candidate as a mixed-evidence GO rather than suppressing
   it.
8. Batch-check the detected rain points with the same Open-Meteo fields for
   diagnostics, but do not use the rain-shaft cloud model as a hard gate. The
   rain shaft is often cloudy because it is raining; the observer-side sunlight
   check is the gate that needs to agree with the browser card.
9. Publish only candidates where all criteria pass:
   low sun, clear-enough observer sky, and rain opposite the sun.

## Free-tier shape

At 5-minute cadence, one run normally uses:

- RainViewer: about 85 requests, including metadata and radar tiles.
- Open-Meteo: usually 2-10 batched requests, depending on `--max-verify`.
- NWS: none in this detector.

That keeps provider load centralized and cacheable, rather than multiplying it by
each site visitor.

## Frontend integration

The map should fetch `candidates.json` and render only `candidates[]` pins. A
clicked pin should pass the candidate's exact `lat`, `lon`, `direction`, and
`evidence` into the card, or the card should re-run the same evaluator against
that exact observer point. ZIP search can still evaluate the ZIP centroid, but a
clicked GO pin should not be reinterpreted as a different ZIP centroid.

## Vercel Hobby + cron-job.org

Vercel Hobby cannot run a 5-minute Vercel Cron, so the deployable path is:

1. Deploy the Vercel function at `/api/candidates`.
2. Configure cron-job.org to request the same URL every 5 minutes:

```text
https://therainbowconnector.com/api/candidates
```

Use these cron-job.org settings:

- Method: `GET`
- Schedule: every 5 minutes
- Timeout: at least 30 seconds, 60 seconds if available
- Expected status: `200`
- No request body

The endpoint returns fresh detector output and includes `diagnostics.gateDiagnostics`
so cloud/radiation false negatives can be audited. It sets:

```text
Cache-Control: public, max-age=0, s-maxage=300, stale-while-revalidate=300
```

The scheduler should hit the exact same URL the frontend fetches, because that
warms Vercel's CDN cache for visitors. Do not add a cache-busting query string
for the scheduled request unless the frontend also uses that same URL.

## Satellite sunlight artifact

`/api/candidates` is now artifact-first and read-only for public traffic. It
checks for a fresh GOES-enriched candidate artifact in this order:

1. Redis key `rainbow:satellite:candidates:latest`.
2. `SATELLITE_CANDIDATES_URL`, useful for a Vercel Blob JSON object that is
   overwritten by the GOES publisher every 5 minutes.
3. Checked-in `satellite-candidates.json`, useful for local testing.
4. Checked-in stale fallback artifact.

During the NOAA migration, only an authenticated cron request with
`skipSatellite=1` may invoke the legacy RainViewer detector. This prevents a
visitor request or a cache miss from starting nationwide weather processing.

The new MRMS-first worker is documented in `worker/README.md`.

Publish a fresh GOES artifact with:

```text
POST /api/satellite-candidates
Authorization: Bearer $SATELLITE_PUBLISH_SECRET
```

`SATELLITE_PUBLISH_SECRET` is preferred; `ALERT_NOTIFY_SECRET` is accepted as a
fallback. The endpoint requires Upstash/Vercel KV credentials and only stores
artifacts whose `expiresAt` is still in the future.

Set `REQUIRE_CANDIDATE_PERSISTENCE=1` after the NOAA worker begins publishing.
The publisher will then hold first-scan hits in `possibleCandidates` and promote
them to GO only when a matching candidate appears in a consecutive scan (or the
candidate carries exceptional multi-source evidence). If an artifact supplies
`sourceHealth`, stale required radar, GOES, or sunlight-model inputs are rejected
instead of being published as live.

For the Blob path, upload the artifact to a stable pathname and set:

```text
SATELLITE_CANDIDATES_URL=https://...public.blob.vercel-storage.com/satellite-candidates.json
```

If upstream weather data fails, `/api/candidates` falls back to the checked-in
`candidates.json` and marks the response with:

```json
{ "stale": true }
```

## Candidate history

Every successful `/api/candidates` detector run is also written to a compact
history log when Upstash/Vercel KV credentials are configured. The same storage
used by email alerts is reused:

```text
UPSTASH_REDIS_REST_URL
UPSTASH_REDIS_REST_TOKEN
```

The history key is `rainbow:candidate:history`. Records are deduplicated by
`generatedAt` and capped with `LTRIM`. Default retention is 288 runs, roughly
24 hours at a 5-minute cadence. Override it with:

```text
CANDIDATE_HISTORY_MAX_RUNS=576
```

Location checks:

```text
/api/candidate-history?zip=48307
/api/candidate-history?lat=42.658&lon=-83.15&radiusKm=55&hours=24
```

The endpoint returns matching GO candidates within the requested radius and time
window, so "was this place a candidate tonight?" can be answered from retained
detector output instead of reconstructed from live provider data.
