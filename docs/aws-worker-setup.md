# AWS setup for the Rainbow Connector worker

## Current production state (July 28, 2026)

The NOAA-first worker is deployed as a Python 3.12 ARM64 Lambda container in
`us-east-1`. CloudFormation stack `rainbow-connector-worker` owns the Lambda,
execution role, 14-day log group, and five-minute EventBridge Scheduler rule.
The schedule is enabled.

The public site remains fast because it never runs the detector during a user
request. Lambda publishes a schema-v3 artifact to Vercel Blob, and
`/api/candidates` only reads that artifact. The obsolete Vercel Python cron is
excluded from production packaging. Two-scan candidate persistence is enforced
against the previous Blob artifact before a candidate can be published as GO.

The verified production image is ARM64 tag `v20260728-dot4` in private ECR.
Its July 28 deployment smoke test completed the detector in 15.6 seconds and
published successfully. The worker requests narrowly targeted DOT-camera jobs
after notifications. Delaware uses HLS; California and Iowa use current JPEGs
first and fall back to HLS where available; Ohio and Washington use current
JPEGs. Camera catalogs are loaded only when a pending GO event is in a supported
state, and camera failure cannot fail the forecast.

Strong radar-only geometry can enter the ordinary POSSIBLE pool when radar
score is at least 90, the observer is dry, rain is within 15 km, and the Sun is
5-22 degrees high. These sunlight-uncertain candidates require two consecutive
scans before public display; the first scan is retained only for persistence
matching and is removed from the public candidate response.

## Expected ongoing cost

At a 13-second billed duration, a 2 GB worker every five minutes uses roughly
232,000 GB-seconds in a 31-day month, below Lambda's ongoing 400,000 GB-second
monthly allowance. A 22-second average is still just below that allowance. A
30-second average is approximately $2.26 per month for compute; a 60-second
average is approximately $11.19. Private ECR storage should be about
$0.05-$0.15 per month after introductory allowances. Actual AWS billing and the
existing $1/$3/$5 budget alerts remain authoritative.

Review cost and usage in January 2027, six months after launch.

## Account security

- Root MFA is enabled. Never create root access keys.
- Everyday access uses IAM user `rainbow-admin` with MFA.
- Local AWS CLI access uses browser-issued temporary credentials, not an access
  key.
- Do not enable AWS Organizations or organization IAM Identity Center while
  promotional credits are active; doing so can expire those credits.
- A monthly $5 gross-usage budget sends alerts at $1, $3, and $5.

Login from this computer with:

```powershell
aws login --profile rainbow-connector --region us-east-1 --remote
aws sts get-caller-identity --profile rainbow-connector
```

## Local build requirements

This ARM64 Windows computer uses:

- AWS CLI v2
- WSL 2
- Docker Desktop with Linux ARM64 containers

SAM CLI is not required. Deployment uses Docker, ECR, and ordinary
CloudFormation commands. `template.yaml` accepts an immutable ECR `ImageUri`.

Build a Lambda-compatible single manifest with provenance disabled:

```powershell
docker build --platform linux/arm64 --provenance=false `
  -f Dockerfile.worker -t rainbow-worker:deploy .
```

Docker provenance must remain disabled because Lambda does not accept the OCI
image index BuildKit otherwise creates for the tag.

## Publishing credential

Lambda and Vercel must share a publishing credential. Production currently
uses the current Vercel `BLOB_READ_WRITE_TOKEN`, which the endpoint accepts as a
fallback credential. The local `.env.production.local` copy can become stale;
pull the current production environment immediately before a credential update:

```powershell
npx vercel env pull C:\tmp\rainbow-vercel-production.env --environment=production
```

Never print, commit, or paste this value into chat. Use a temporary
CloudFormation parameter file, overwrite it, and delete it immediately after
the update. A later hardening step should create a dedicated
`SATELLITE_PUBLISH_SECRET` in both Vercel and Lambda so the worker does not need
the broader Blob token.

## Deployment and validation

1. Run the Python worker tests and Node policy/science/integration tests.
2. Build with `--platform linux/arm64 --provenance=false`.
3. Smoke-test scientific imports inside the image.
4. Push a new immutable ECR tag; never replace an existing tag.
5. Update the CloudFormation `ImageUri` parameter and wait for
   `UPDATE_COMPLETE`.
6. Invoke Lambda once manually while the schedule is disabled.
7. Verify the public artifact's schema, source health, and load time.
8. Enable the schedule through CloudFormation and verify two consecutive
   scheduled artifacts, including `persistenceDiagnostics`.

The required source limits are:

- NOAA MRMS radar: 6 minutes
- GOES ACMC cloud mask: 20 minutes
- GOES DSRF irradiance context: 30 minutes
- Open-Meteo instantaneous DNI: 30 minutes

The API fails closed and preserves the last healthy artifact if a required
source is stale or missing.

## Monitoring and rollback

Check stack, Lambda, and schedule state:

```powershell
aws cloudformation describe-stacks --stack-name rainbow-connector-worker `
  --profile rainbow-connector --region us-east-1

aws lambda get-function-configuration `
  --function-name rainbow-connector-worker-RainbowWorker-GsIQyBOI66sc `
  --profile rainbow-connector --region us-east-1
```

For an immediate pause, change the schedule `State` in `template.yaml` to
`DISABLED` and update the CloudFormation stack. Do not delete the ECR image or
Vercel Blob artifact during a pause; the site can continue serving the most
recent healthy artifact until it expires.

ECR basic scanning reported two HIGH findings inherited from the current AWS
Lambda Amazon Linux base image: `libacl` CVE-2026-54369 and `glib2`
CVE-2026-58016. Amazon's repositories had no patched versions at deployment.
The function has no inbound endpoint, `libacl` requires a local authenticated
attacker, and the worker does not invoke `glib2`, so deployment proceeded with
the findings documented. Rebuild and rescan when AWS publishes an updated base
image.
