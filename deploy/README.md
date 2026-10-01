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

### Open signup: the client sign-in authority (#91)

`GET /api/config` now returns the MSAL `authority` for the pane and the sign-in
dialog: the home tenant in `SIGNUP_MODE=allowlist` (unchanged), and
`https://login.microsoftonline.com/common` in `open` mode. For personal
Microsoft accounts to work in `open` mode, the Entra app registration must be
set to "Accounts in any organizational directory and personal Microsoft
accounts" and have `requestedAccessTokenVersion` = 2 in its manifest (the older
manifest format calls it `accessTokenAcceptedVersion`; otherwise
personal-account tokens carry a v1 issuer and are rejected). Some other
organisations block user consent, so their people will see an admin-consent
prompt; that is expected. Optionally set `PRIVACY_URL` and `TERMS_URL` (https)
to show privacy and terms links before first sign-in. After changing
`SIGNUP_MODE`, people who are already signed in should sign out and back in,
because their cached account belongs to the old authority.

### Going live with open signup (#85, #92)

Don't set `SIGNUP_MODE=open` until the go-live checklist in `PLAN.md`
section 3 is complete. In short: the Entra app registration changes (also in
`PLAN.md`), privacy policy and terms published and set as `PRIVACY_URL` /
`TERMS_URL`, account deletion available, backup retention decided
([docs/BACKUP.md](../docs/BACKUP.md)), proxy log retention and DDoS protection
reviewed, and a test with a personal Microsoft account and a second tenant.
Then in `deploy/compose.yaml`:

- `SIGNUP_MODE: "open"` (needs `AUTH_MODE: entra` and `ENTRA_TENANT_ID` set to
  the home tenant's GUID; `ALLOWED_OIDS` becomes optional).
- `BLOCKED_TENANTS`: tenant ids to refuse, if any.
- `MAX_USERS`: a cap on total accounts (`0` = unlimited). Once reached, people
  who have never signed in get a 403; existing accounts are unaffected.
- The other limits (`MAX_BYTES_PER_OWNER`, `MAX_SERIES_PER_OWNER`,
  `MAX_RECORDS_PER_SERIES`, `WRITE_RATE_LIMIT_PER_MINUTE`,
  `FEEDBACK_RATE_LIMIT_*`) have defaults; see `.env.example` before changing.

Redeploy, then check `/api/config` reports the `common` authority. To roll
back set `SIGNUP_MODE: "allowlist"` again; accounts already created are kept
and `ALLOWED_OIDS` applies again.

### Administrators: viewing and blocking users (#82)

Set `ADMIN_OIDS` to a comma-separated list of the Entra object ids of the
people who should administer the server (your own `oid` is shown by
`GET /api/me`). They get an **Admin** button in the task pane listing everyone
who has signed in - name, email, last active, how many series and records they
own and how much space they use (never story content) - with Block/Unblock.
A blocked person is refused (403) on their next request; their data is kept.
Use object ids, not emails: with open signup an email can be forged. Unset
means nobody is an administrator.
