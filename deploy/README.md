# Deploying to TrueNAS

Everything on this page happens on the TrueNAS box itself (SSH or the
TrueNAS UI) - none of it can be done from this repo/CI, and none of it has
been run yet as part of building these files (#6). Treat this as the
runbook, not a record of a completed deployment.

## 1. Dataset

Create a dataset for the app's persistent data, owned by the TrueNAS SCALE
"apps" user/group (568:568) - the container already runs as that user (see
the Dockerfile), so this is what makes the bind mount just work with no
extra `chown` step:

```
zfs create tank/apps/storybible
chown 568:568 /mnt/tank/apps/storybible
```

(Adjust `tank` to your actual pool name.)

## 2. Clone the repo

```
git clone https://github.com/GazmanDevelopment/Story-Bible.git /mnt/tank/apps/storybible-src
cd /mnt/tank/apps/storybible-src
```

(`.env.example` here documents every setting the app reads, for reference -
see steps 3-4 for where the values actually go for this deployment path.)

## 3. Build the image

```
sh deploy/update.sh
```

This builds `story-bible:<version>` and tags it `story-bible:current`, and
(first run only) creates `deploy/compose.yaml` from `deploy/compose.yaml.example`
if it doesn't exist yet. It's also what you re-run for every future update
(`git pull` + rebuild) - see the comment at the top of the script for
rollback.

**`deploy/compose.yaml` is gitignored - edit real values into it, never
into `deploy/compose.yaml.example`.** That split exists specifically so
`git pull` (every future run of this same script) never collides with your
local edits (#38) - a tracked file with real secrets edited into it would
conflict with the next pull, every time.

## 4. Create the Custom App

Edit `deploy/compose.yaml` (created in step 3): adjust the dataset path in
`volumes:` if you used a different pool/path than step 1, set
`STORYBIBLE_TOKEN` to a real secret (see the comment above it in the YAML
for how to generate one), and set `FORWARDED_ALLOW_IPS` to the Synology
reverse proxy's LAN IP (#7).

Then, TrueNAS UI → **Apps → Discover Apps** → (top-right) **Install via
YAML** → paste `deploy/compose.yaml`'s contents (the edited one, with real
values - this dialog doesn't accept a separate `.env` file, #36) → install.

The app listens on host port **2285** (port 8000 on this NAS is already in
use by something else - see the #6/#7 issue comments), mapped from the
container's internal 8000.

## 5. Point the reverse proxy at it

Already done per #7: the Synology reverse proxy forwards
`storybible.huscroft.com.au` to `http://<truenas-ip>:2285`.

## Network exposure: only the reverse proxy should reach port 2285

The Custom App publishes **plain HTTP on host port 2285 on every network
interface of the NAS**. The proxy in step 5 puts TLS in front of it, but nothing
stops another machine on your LAN (or VPN) from talking to `http://<nas-ip>:2285`
directly - and anything sent that way, including the sign-in token, travels
unencrypted, bypassing the proxy entirely.

Close that door so only the Synology can reach the port:

- **Firewall rule (recommended).** On your router / firewall (or a VLAN rule),
  allow `<synology-ip> -> <nas-ip>:2285` and block everything else to that port.
  Use whatever device enforces traffic between machines on your network; this
  repo doesn't assume one.
- **If the NAS has several network interfaces**, bind the port to the one the
  proxy uses instead of all of them, by changing the mapping in the Custom App
  to `"<nas-lan-ip>:2285:8000"`. This narrows *which interface*; it does not
  narrow *which machines on that network* - it complements the firewall rule,
  it doesn't replace it.
- The app itself only trusts `X-Forwarded-*` from the proxy's address
  (`FORWARDED_ALLOW_IPS`, step 4), so a request that skips the proxy is not
  mistaken for one that came through it.

**Check it worked:** from a machine that is *not* the Synology, run
`curl -m 5 http://<nas-ip>:2285/api/health`. It should time out or be refused. From
the Synology (or via `https://storybible...`) it should answer.

## 6. Verify

```
curl https://storybible.huscroft.com.au/api/health
```

should return `{"ok": true, ...}`. If it doesn't, check the Custom App's
logs in the TrueNAS UI - `HardeningMiddleware` (`app/main.py`) logs every
request to stdout, which is what those logs show.

## Snapshots and backups

The app backs up its own database nightly (#5 - see `docs/BACKUP.md`), but
that lives *inside* the same dataset. Add a periodic ZFS snapshot task on
`tank/apps/storybible` too, so there's a copy outside the dataset entirely -
see `docs/BACKUP.md`'s "TrueNAS: snapshot the dataset too" section.

## Monitoring

Point an uptime checker at `/api/health` - its status code for the server,
its `backup_ok` field for backups (#17, #68),
and confirm TrueNAS alerting is actually wired to a notification service
so a failed snapshot task above doesn't go unnoticed either - see
`docs/MONITORING.md` for the exact setup.

## Upgrading: stricter Entra settings (#70)

Two settings changed for `AUTH_MODE=entra`. **Set both before deploying this
version**, or the container refuses to start (a crash loop with a clear
`RuntimeError` in the logs):

- `ALLOWED_OIDS` is now **required**: a comma-separated list of the Entra
  object ids allowed in (yours, and anyone you share with). It used to be
  optional, and leaving it blank silently let in every tenant user Entra
  itself admitted. `ALLOWED_OIDS=*` deliberately restores "allow everyone
  Entra lets in" (not recommended). The review pipeline's app-role token
  isn't subject to this list.
- `LEGACY_OWNER_OID` (optional): the object id of whoever should own any
  series created before sign-in existed. Previously the first person to
  sign in got them all, which meant any unintended tenant user could take
  them. If any such series still exist and this is unset, the server logs a
  warning at startup and nobody signed in can see them. If you signed in
  after #11 shipped they were already claimed and you need do nothing.
