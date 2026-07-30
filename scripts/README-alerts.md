# Rainbow email alerts

Email alerts use two small services:

- Upstash Redis for confirmed subscriptions and cooldown state.
- Resend for confirmation and GO-candidate alert emails.

Required Vercel environment variables:

```text
UPSTASH_REDIS_REST_URL=...
UPSTASH_REDIS_REST_TOKEN=...
RESEND_API_KEY=...
RESEND_FROM=The Rainbow Connector <alerts@therainbowconnector.com>
ALERT_NOTIFY_SECRET=long-random-secret
ALERT_BASE_URL=https://therainbowconnector.com
TURNSTILE_SITE_KEY=...
TURNSTILE_SECRET_KEY=...
TURNSTILE_ALLOWED_HOSTNAMES=therainbowconnector.com,www.therainbowconnector.com
```

Optional controls:

```text
ALERT_COOLDOWN_HOURS=12
ALERT_MAX_SENDS_PER_RUN=25
ALERT_CONFIRM_TTL_SECONDS=259200
```

Flow:

1. The public form obtains a Cloudflare Turnstile token and posts `{ email, zip, turnstileToken }` to `/api/alerts`.
2. `/api/alerts` validates that single-use token server-side, enforces IP/email rate limits, validates the ZIP, stores a short-lived confirmation token, and sends a Resend confirmation email.
3. The confirmation link activates the subscription in Redis.
4. The AWS worker publishes its scan to `/api/satellite-candidates`, where consecutive-scan persistence is applied.
5. After that publish succeeds, the worker calls `/api/notify-alerts` with `Authorization: Bearer $ALERT_NOTIFY_SECRET` and no candidate payload.
6. `/api/notify-alerts` loads the fresh, finalized artifact, accepts strict GO candidates immediately (including a first scan), matches subscriptions within the fixed 15 km ZIP policy, sends at most `ALERT_MAX_SENDS_PER_RUN`, and updates each subscription cooldown so confirmation does not send a duplicate.

Required AWS worker parameters/environment:

```text
RAINBOW_NOTIFY_URL=https://therainbowconnector.com/api/notify-alerts
ALERT_NOTIFY_SECRET=the-same-long-random-secret-used-in-vercel
```

The notification call is skipped safely until both values are configured.

Example scheduler request:

```bash
curl -X POST https://therainbowconnector.com/api/notify-alerts \
  -H "Authorization: Bearer $ALERT_NOTIFY_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"dryRun":false}'
```

Dry run:

```bash
curl -X POST https://therainbowconnector.com/api/notify-alerts \
  -H "Authorization: Bearer $ALERT_NOTIFY_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"dryRun":true}'
```
