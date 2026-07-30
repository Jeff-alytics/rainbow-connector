# NOAA-first candidate worker

This directory contains the compute-heavy half of The Rainbow Connector. It is
intended for a scheduled Lambda or container, separate from the visitor-facing
Vercel API.

## Pipeline

1. Discover the newest NOAA MRMS CONUS precipitation-rate object.
2. Reject radar older than six minutes.
3. Decode the national GRIB2 grid once.
4. Find precipitation edges and place observer seeds on their sunward side,
   retaining only physical land in the contiguous United States. The rain cell
   may remain offshore so coastal rainbows are not lost.
5. Batch-check only those seeds for instantaneous DNI.
6. Corroborate the sunward sector with one GOES East/West cloud-mask download.
7. Apply terrain, ranking, and consecutive-scan persistence.
8. Publish a small candidate artifact to `/api/satellite-candidates`.

Steps 1-8 are now implemented. The GOES sampler is shared with `api/` so the
worker and existing satellite validation path use the same product semantics.
The deployment schedule in `template.yaml` is disabled by default for shadow
testing.

## Local smoke tests

```powershell
uv pip install --python .\.venv\Scripts\python.exe -r worker\requirements.txt
.\.venv\Scripts\python.exe worker\build_noaa_candidates.py --metadata-only
.\.venv\Scripts\python.exe worker\build_noaa_candidates.py --maximum 50 --stride 12
.\.venv\Scripts\python.exe worker\pipeline.py --maximum 50 --stride 12
python -m unittest discover -s worker -p "test_*.py" -v
```

`build_noaa_candidates.py` outputs the radar shortlist. `pipeline.py` outputs a
publishable schema-v3 artifact after DNI and GOES checks. A zero-item shortlist
is valid when current rain is outside the 0-30 degree solar window.

## Fast visitor path

The browser never downloads or decodes MRMS or GOES. It requests the cached
artifact from `/api/candidates`; the endpoint returns Redis/Blob/CDN data and
serves a marked stale artifact if no fresh result exists. Only an authenticated
cron request can invoke the legacy detector during the migration.
