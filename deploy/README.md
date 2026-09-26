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

## 6. Verify

```
curl https://storybible.huscroft.com.au/api/health
```

should return `{"ok": true, ...}`. If it doesn't, check the Custom App's
logs in the TrueNAS UI - `hardening_middleware` (`app/main.py`) logs every
request to stdout, which is what those logs show.

## Snapshots and backups

The app backs up its own database nightly (#5 - see `docs/BACKUP.md`), but
that lives *inside* the same dataset. Add a periodic ZFS snapshot task on
`tank/apps/storybible` too, so there's a copy outside the dataset entirely -
see `docs/BACKUP.md`'s "TrueNAS: snapshot the dataset too" section.
