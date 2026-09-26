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
see step 4 for where the values actually go for this deployment path.)

## 3. Build the image

```
sh deploy/update.sh
```

This builds `story-bible:<version>` and tags it `story-bible:current`. It's
also what you re-run for every future update (`git pull` + rebuild) -
see the comment at the top of the script for rollback.

## 4. Create the Custom App

TrueNAS UI → **Apps → Discover Apps** → (top-right) **Install via YAML** →
paste the contents of `deploy/compose.yaml`, then, **in the pasted text
itself** (this dialog doesn't accept a separate `.env` file - #36):

- adjust the dataset path in `volumes:` if you used a different pool/path
  than step 1
- set `STORYBIBLE_TOKEN` to a real secret (see the comment above it in the
  YAML for how to generate one)
- set `FORWARDED_ALLOW_IPS` to the Synology reverse proxy's LAN IP (#7)

then install. Don't paste those real values back into a copy of
`compose.yaml` that gets committed to git - keep the checked-in file's
placeholders as they are.

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
