# Monitoring (#17)

Two things need to alert on their own, without anyone having to remember to
check: the server going down, and backups silently stopping. Both have a
dedicated, unauthenticated health endpoint for exactly this - point an
uptime checker at them rather than inferring health from application logs.

## What to monitor

| Check | URL | Healthy | Unhealthy |
|---|---|---|---|
| Server up | `https://storybible.huscroft.com.au/api/health` | `200` | `503` - database unreachable or `/data` not writable |
| Backup freshness | `https://storybible.huscroft.com.au/api/health/backup` | `200` | `503` - no successful backup yet, or the last one is over 36h old |

Both:
- Take **no authentication** - a monitoring tool has no `STORYBIBLE_TOKEN`/
  Entra token, so these two routes are deliberately exempt (see
  `app/main.py`). Nothing they return is sensitive: a bare `ok`/`error`
  and, for `/api/health`, the running version.
- Are **never cached** (`Cache-Control: no-store`) - a stale cached `200`
  from a proxy in between would defeat the whole point.
- Return a real HTTP status code (`200`/`503`), not just `"ok": false` in
  a `200` body - so a plain "is this URL up" check (not just a keyword
  monitor) already works.

The 36-hour backup threshold (`MAX_BACKUP_AGE_SECONDS` in `app/main.py`)
gives a full extra day of slack over the once-daily backup schedule before
alerting, so one delayed run doesn't page anyone - see
[BACKUP.md](BACKUP.md) for what actually runs and when.

## Uptime Kuma

Available as a TrueNAS SCALE catalog app if you don't already have an
instance running somewhere (Apps → Discover Apps → search "Uptime Kuma") -
give it its own small dataset for `/app/data`, same pattern as this app's
own dataset.

For each of the two URLs above, add a monitor:

1. **Add New Monitor** → Monitor Type: **HTTP(s) - Status code** (not
   "keyword" - the status code alone is the whole signal here).
2. **Friendly Name**: `Story Bible - server` / `Story Bible - backup freshness`.
3. **URL**: from the table above.
4. **Heartbeat Interval**: 60s for the server check is plenty; the backup
   check only needs to catch drift over hours, so every 5-10 minutes is
   fine and cuts log noise.
5. **Retries**: set to 2-3 with a short **Heartbeat Retry Interval** (e.g.
   20s) before Kuma actually fires a notification - a single dropped
   request over a flaky LAN link shouldn't page anyone; two or three
   consecutive failures should.
6. **Notifications**: attach a notification method (below) to both
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
