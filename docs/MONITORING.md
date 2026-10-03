# Monitoring (#17)

Two things need to alert on their own, without anyone having to remember to
check: the server going down, and backups silently stopping. Both are
reported by one unauthenticated endpoint, `/api/health` - point an uptime
checker at it rather than inferring health from application logs. (Backup
freshness used to be a separate `/api/health/backup` route; it was folded
in so `/api/health` is the only public API route - #68. A monitor still
pointed at the old URL now gets a 404, which is your cue to update it.)

## What to monitor

`https://storybible.huscroft.com.au/api/health` returns, for example:

```json
{"ok":true,"auth":true,"version":"0.1.0","build":"v0.1.0-12-gabc1234","buildDate":"2026-10-03","backup_ok":true}
```

| Check | How | Healthy | Unhealthy |
|---|---|---|---|
| Server up | HTTP status of `/api/health` | `200` | `503` - database unreachable or `/data` not writable |
| Backup freshness | the `backup_ok` field in the same response | `"backup_ok": true` | `"backup_ok": false` - no successful backup yet, or the last one is over 36h old |

`backup_ok` deliberately does **not** change the HTTP status: Docker's
built-in healthcheck restarts the container on any non-200, and a brand-new
install has no backup for up to a day. So the backup check has to look at
the response body (a keyword monitor), not just the status code.

The endpoint:
- Takes **no authentication** - a monitoring tool has no `STORYBIBLE_TOKEN`/
  Entra token, so this route is deliberately exempt (see `app/main.py`).
  Nothing it returns is sensitive: `ok`, `auth`, the running version and
  the `backup_ok` boolean (the exact backup age is no longer exposed).
- Is **never cached** by clients or proxies (`Cache-Control: no-store`) - a
  stale cached `200` from a proxy in between would defeat the whole point.
  (The server itself re-probes the data directory at most every 5 seconds.)
- Returns a real HTTP status code (`200`/`503`) for the server check, not
  just `"ok": false` in a `200` body.

The 36-hour backup threshold (`MAX_BACKUP_AGE_SECONDS` in `app/main.py`)
gives a full extra day of slack over the once-daily backup schedule before
alerting, so one delayed run doesn't page anyone - see
[BACKUP.md](BACKUP.md) for what actually runs and when.

## Uptime Kuma

Available as a TrueNAS SCALE catalog app if you don't already have an
instance running somewhere (Apps → Discover Apps → search "Uptime Kuma") -
give it its own small dataset for `/app/data`, same pattern as this app's
own dataset.

Add two monitors against the same URL, `https://storybible.huscroft.com.au/api/health`:

1. **Server**: Monitor Type **HTTP(s) - Status code**, Friendly Name
   `Story Bible - server`. Heartbeat Interval 60s is plenty.
2. **Backup freshness**: Monitor Type **HTTP(s) - Keyword**, Friendly Name
   `Story Bible - backup freshness`, Keyword `"backup_ok":true` (exactly
   that - the response is compact JSON, no spaces; Kuma alerts when it is
   *missing*). The
   backup check only needs to catch drift over hours, so a 5-10 minute
   interval is fine and cuts log noise.

For both:

3. **Retries**: set to 2-3 with a short **Heartbeat Retry Interval** (e.g.
   20s) before Kuma actually fires a notification - a single dropped
   request over a flaky LAN link shouldn't page anyone; two or three
   consecutive failures should.
4. **Notifications**: attach a notification method (below) to both
   monitors before saving.

### Notifications

Settings → **Notifications** → Add a notification, then attach it to both
monitors above (a notification isn't active on a monitor just because it
exists - each monitor has its own checklist of which notifications fire
for it). Pick whatever you'll actually see promptly:

- **Email (SMTP)** - reuses any mailbox you already have; no extra
  service to sign up for.
- **A push service** (Pushover, ntfy, ForceAlert, ... - Kuma supports
  quite a few) if you want something that reaches a phone immediately
  rather than sitting in an inbox.

Either is fine; what matters is that it's *not* email-only if that inbox
isn't checked often, since the entire point of this issue is "the worst
case is a backup that silently stopped months ago."

## TrueNAS: snapshot task failure alerts

This covers the app noticing its own backup job failed. It doesn't cover
the periodic ZFS snapshot task on the dataset itself (see
[BACKUP.md](BACKUP.md#truenas-snapshot-the-dataset-too)) failing -
TrueNAS handles that itself, but only if alerting is actually wired up:

1. **System Settings → Alert Settings**: confirm at least one **Alert
   Service** is configured (email is the built-in default) - a failed
   snapshot task raises a TrueNAS alert automatically, but an alert with
   nowhere to go is as good as no alert.
2. **Data Protection → Periodic Snapshot Tasks**: confirm the task on the
   app's dataset (e.g. `tank/apps/storybible`) exists and is enabled - no
   special "notify on failure" checkbox needed on the task itself, that's
   what step 1's global Alert Settings covers.
3. Optional sanity check: **System Settings → Alert Settings → Test
   Alert** sends a test notification through every configured service, so
   you know the path actually works before relying on it.
