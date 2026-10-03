# Personal data breach checklist

A personal data breach is any security incident that destroys, loses, alters,
discloses or gives access to personal data by accident or on purpose: a leaked
database or backup, an account taken over, a bug that showed one person's
series to another, a lost laptop or disk holding backups, a leaked secret that
could have been used to read data. The privacy policy promises to report to the
ICO **within 72 hours of becoming aware** of a breach that puts people's rights
at risk, and to tell affected people without undue delay when the risk to them
is high. What is held and where: [PROCESSING.md](PROCESSING.md).

**Start the clock when you become aware.** Write the time down first. 72 hours
includes weekends. If you can't tell yet whether it counts, treat it as one and
keep going while you find out; the ICO accepts a notification in stages.

## 1. Contain (first hour)

- [ ] Stop the harm: block the affected person (the pane's Admin view, or by hand
  if you can't sign in: `sqlite3 /data/storybible.db "INSERT OR REPLACE INTO
  blocked_users (oid, blocked_at, blocked_by, reason) VALUES ('<oid>', strftime('%s','now'),
  'operator', 'breach')"`, then restart so the in-memory sign-in cache is cleared),
  block the tenant (`BLOCKED_TENANTS`), or take the app offline (stop the
  container; the data stays safe in the database).
- [ ] If a secret may have leaked, replace it now:
  - `GITHUB_FEEDBACK_TOKEN`: revoke the fine-grained token on GitHub, create a new one;
  - `SMTP_PASSWORD`: revoke the app password at the mail provider, create a new one;
  - `STORYBIBLE_TOKEN` (token mode only): set a new value;
  - the Entra app registration: there is no client secret, but check the
    registration for unexpected changes and review sign-in logs in the Entra portal.
  Redeploy with the new values.
- [ ] If the server itself may be compromised: take it offline, don't clean it
  up until you have copied the evidence (logs, the database, `/data/backups`),
  and rebuild from a known-good image and a backup that predates the incident
  ([RESTORE.md](RESTORE.md), including the tombstone step so erased accounts stay erased).
- [ ] Keep evidence: container and proxy logs (they roll off, see PROCESSING.md),
  the time of discovery, screenshots, the commit or config that caused it.

## 2. Assess

- [ ] What happened, when it started and ended, how it was found.
- [ ] Which data: account details (name, email, object id), story content,
  research images, sharing information, backups?
- [ ] Whose and how many people. The `users` table, `series.owner_oid` and
  `members` tell you who could have been affected.
- [ ] Was it readable by someone who shouldn't have it (disclosure), altered,
  or lost? Was the data encrypted or otherwise unintelligible to them? (Backups
  are plaintext, owner-only files.)
- [ ] Likely harm to those people. Story bibles can contain real names and
  private or sensitive material; a leak of one is more than an inconvenience.
  Decide: **unlikely to result in a risk**, **risk**, or **high risk**.

## 3. Record it (always)

Add an entry to the breach log, even if nothing is reported: date and time of
discovery, what happened, data and people affected, the risk decision and why,
who was told and when, what was done. Keep the log with the other operational
records. If you decide not to notify, the reasons must be written down.

## 4. Notify

- [ ] **ICO, within 72 hours of becoming aware, if there is a risk to people's
  rights and freedoms.** Report online at <https://ico.org.uk/for-organisations/report-a-breach/> (check the link still works - it is the "Report a breach" page for organisations, not the complaints form for individuals)
  (or by phone on 0303 123 1113 during office hours). Give: what happened; the
  categories and approximate numbers of people and records; the likely
  consequences; what you have done and will do; a contact. If you don't have
  everything yet, send what you have and say when you'll follow up.
- [ ] **The people affected, without undue delay, if the risk is high.** Plain
  language, by the email on their account if there is a usable one (otherwise
  in the app): what happened, what data, what it may mean for them, what you
  have done, what they can do (for example, change passwords and review sign-ins
  at Microsoft), and a contact address.
- [ ] Other parties as appropriate: Microsoft (if sign-in was involved), GitHub
  (a leaked token or personal data in an issue: ask for the issue to be removed),
  the mail provider.

## 5. Recover and learn

- [ ] Fix the cause; add a regression test if it was a code bug.
- [ ] If the policy or its promises were wrong, correct them and bump
  `POLICY_VERSION` so people are asked to accept again.
- [ ] Update the breach log entry with the outcome and what changed
  ([SECURITY.md](../SECURITY.md) and [PROCESSING.md](PROCESSING.md) if the
  threat model or data held did).
