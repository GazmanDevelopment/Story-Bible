# Record of processing

An internal record of the personal data Story Bible holds, why, on what basis,
who else receives it, how long it is kept and how it is protected. It backs up
what the published [privacy policy](privacy.html) says and is the document to
check (and update) when either the code or the policy changes. Not legal
advice; have it checked before the public announcement.

- **Controller:** Gareth Huscroft (named in the privacy policy, with the
  contact address published there). UK GDPR.
- **Administrator:** the same person. Anyone who can reach the database file
  on the server can read everything in it; [SECURITY.md](../SECURITY.md)
  records that as accepted and the policy discloses it.
- **Applies when:** `AUTH_MODE=entra`. In `none`/`token` mode there is one
  shared bible and no personal accounts.

## What is held

| Data | Where | Purpose | Lawful basis | Recipients | Kept for |
|---|---|---|---|---|---|
| Account: Entra object id (`oid`), tenant id, display name, email / `preferred_username`, first and last seen | `users` table | Know who someone is and which series are theirs; sign-in | Performance of the service (Art. 6(1)(b)) | Administrator; people who share a series see names | Until the account is deleted by the person or for inactivity (below) |
| Policy acceptance: version accepted, when | `users` table | Record that the person was shown the terms and policy | Legitimate interests (6(1)(f)): evidence of notice. Acknowledgement, not consent | Administrator | With the account |
| Inactive-account notice stage | `users` table | Send each reminder once | Legitimate interests (6(1)(f)) | Administrator | With the account |
| Content: series, characters, places, events, relationships, research (incl. embedded images), chapters | `series`, `records` tables | The service itself | Performance of the service (6(1)(b)) | Administrator; people the owner shares the series with | Until deleted by the owner, or with the account |
| Sharing and audit: members (oid, role), `created_by` / `updated_by` on every row | `members`, `series`, `records` | Sharing; show who edited what | Performance of the service (6(1)(b)) | People who share the series (names only) | With the series. On account deletion a person's name on other people's rows becomes "Deleted user" |
| Blocks: oid, when, by whom, reason | `blocked_users` | Enforce an administrator's block (abuse prevention) | Legitimate interests (6(1)(f)) | Administrator | Kept after account deletion, so deleting is no way round a block |
| Deletion tombstones: oid and time only | `deleted_users` | Re-apply erasure after a restore from backup | Legal obligation / legitimate interests: honouring erasure | Administrator | Kept (no other data) |
| Backups: nightly full database copy plus a JSON export of every series, plaintext, owner-only file permissions | `BACKUP_DIR` (default `<data>/backups`) | Recovery | Legitimate interests (6(1)(f)): availability | Administrator | `BACKUP_KEEP_DAYS`, default **14**. A deleted account stays in backups until they age out |
| Feedback filed from the pane: title, description, the person's display name (never email or oid) | Public GitHub issue | Bug reports and suggestions the person chose to send | The person's own act of submitting; legitimate interests (6(1)(f)) | **GitHub** (public, world-readable) | Not removed on account deletion; removal on request |
| Request logs: time, method, path, status, duration; plus the client IP in proxy/container logs | stdout / container logs; reverse proxy logs | Keep the service working and secure | Legitimate interests (6(1)(f)): security and operation | Administrator | **30 days at most** (see below) |
| MSAL token cache | The person's own browser `localStorage` | Stay signed in. Strictly necessary; no tracking or analytics | Strictly necessary | None | Until sign-out |

The app never writes request bodies, `Authorization` or `X-Token` headers, or
story content to its logs (`HardeningMiddleware` logs method, path, status and
time only).

## Who else receives data

| Recipient | What | Why | Notes |
|---|---|---|---|
| Microsoft (Entra) | Sign-in | Authentication | Sends us the name, email and object id. Own privacy policy |
| GitHub | Feedback text and display name | Issue tracking | Public repository |
| Mail provider (the `SMTP_HOST` account used for notices) | Recipient email address, notice text | Inactive-account notices (`app/retention.py`) | Only when mail is configured. **The privacy policy's "other services" section does not currently name it** - see "Open points" |
| Hosting | The server and any off-box backup copies | Running the service | Self-hosted; where it physically sits matters for transfers - see "Open points" |

No analytics, advertising, or data sale.

## Retention summary

- Live data: until the person deletes it or the account (`DELETE /api/me`),
  which removes the account, every series they own and their memberships
  straight away.
- Inactive accounts: deleted after `INACTIVE_DELETE_MONTHS` (24); emailed at
  `INACTIVE_NOTICE_MONTHS` (18, 20, 23) when a usable address exists
  (`app/retention.py`). The deletion writes the same tombstone.
- Backups: `BACKUP_KEEP_DAYS` = 14 in the code, `.env.example` and
  [BACKUP.md](BACKUP.md); the deploy example doesn't override it. Don't keep
  snapshots or off-box copies longer than the policy states.
- Restore: after any restore, re-apply deletions and blocks
  ([RESTORE.md](RESTORE.md), `scripts/apply_tombstones.py`).
- Logs: see below.

## Security measures

Sign-in via Entra with per-tenant token validation; per-series ownership and
roles; the API enforces policy acceptance; request-size, rate and storage
limits; blocked tenants and people; security headers and a restrictive CSP;
no request bodies or tokens in logs; owner-only backup files; a read-only
container filesystem and a non-root user ([SECURITY.md](../SECURITY.md),
[deploy/README.md](../deploy/README.md)). Breaches: [BREACH.md](BREACH.md).

## Logs and IP addresses

IP addresses are personal data. The privacy policy says logs are kept for no
more than **30 days**; the operator has to make that true in each place logs
land:

1. **Application / container logs** (what `docker logs` and the TrueNAS app
   log view show). The app logs method, path, status and time, not IPs, but
   the platform may add them. Docker's default `json-file` driver rotates by
   size, not age, so set `max-size` and `max-file` small enough that, at this
   server's traffic, the files cover well under 30 days, and check after a
   month that the oldest line is under 30 days old.
2. **Reverse proxy logs** (the Synology proxy's access logs). These hold client
   IPs. Set its log rotation/retention to 30 days or less (or turn access
   logging down), and confirm the oldest entry is within 30 days.
3. **Anything else that copies logs** (a log shipper, a monitoring tool,
   snapshots of the log volume): same limit, or don't copy them.

Record here, with the date, what was set and where, and re-check after
platform upgrades:

| Where | Setting | Confirmed on |
|---|---|---|
| Container / TrueNAS app log | _to fill in_ | |
| Synology reverse proxy | _to fill in_ | |

## UK ICO data protection fee

Organisations that process personal data and are not exempt must pay the ICO
data protection fee (and are listed on the register). Whether a free service
run by one person for other people's accounts is exempt is a question the
ICO's own self-assessment answers:
<https://ico.org.uk/for-organisations/data-protection-fee/self-assessment/>

The service is offered to the public, so the "purely personal / household"
exemption does not apply; there is no staff, but there are users' accounts and
content. The self-assessment is the deciding step, and its answer is to be
recorded here by the controller:

| Question | Answer |
|---|---|
| Date self-assessment run | _to fill in_ |
| Outcome (fee payable / exempt, and which exemption) | _to fill in_ |
| If payable: tier, registration number, renewal date | _to fill in_ |

## Open points

Found while writing this record; each needs a decision, and the policy may
need a version bump (and `POLICY_VERSION`) if it changes.

- **Mail provider not disclosed.** The policy says data is not shared with
  anyone else beyond Microsoft and GitHub, but inactive-account notices pass
  the person's address to whichever provider runs `SMTP_HOST`. Name it in the
  policy (or stop sending mail).
- **Where the server sits.** The policy says the data is on a server run by the
  administrator but not in which country. If it is outside the UK, say so, and
  check whether a UK-to-that-country transfer needs a safeguard.
- **Log retention values** in the table above are unrecorded until the
  operator fills them in.
