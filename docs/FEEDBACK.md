# Filing feedback from the pane

The Series tab's **Log Issue** / **Log Suggestion** buttons file a GitHub
issue directly against this repo, from inside Word (or the browser demo) -
no GitHub account needed for whoever's using the pane.

## How it works

The pane sends the title/description you type to `POST /api/feedback` (the
same sign-in every other `/api/*` call already needs - it isn't a new,
separately-open endpoint). The **server** holds a
GitHub token and files the issue on your behalf:

- **Log Issue** → labelled `bug`, `area:pane`
- **Log Suggestion** → labelled `enhancement`, `area:pane`

A short footer is appended to the issue body noting it was filed from the
pane, the app version, and when. In Entra mode it also says
`Submitted by <your display name>`.

**The repository is public, so everything filed here is world-readable.** The
form says so, next to the description box. Only the *display name* is added -
never your email address or object id - and it is cleaned first (letters,
digits, spaces and a little punctuation; no markdown, mentions or links). In
`none`/`token` mode there is no real identity to name, so nothing is added.

## Setup: `GITHUB_FEEDBACK_TOKEN`

A fine-grained GitHub PAT, scoped to **just this repo**, with **Issues:
write** permission only - nothing else. Set it in `deploy/compose.yaml`
(see `.env.example`) alongside the other settings. Without it, filing
returns a clear 503 ("Feedback filing isn't configured on this server")
rather than crashing - same defensive pattern as the health checks.

This token never reaches the client: the pane only ever calls
`fetch("/api"+path)` (never a third-party API directly), and it isn't
referenced by any static file or the manifest.

## Rate limiting

A small in-process limit guards against an accidental double-submit or one
misbehaving account, not abuse from a stranger - this endpoint sits behind
sign-in, and the deployment is LAN/VPN-only (see `SECURITY.md`):

- **5 filings per person per hour**, so one person can't use up another's
  allowance (in `none`/`token` mode everyone shares one identity, so it is
  effectively 5 in total).
- **20 per hour across everyone**, as a ceiling.
- **Only filings that actually reached GitHub count.** If GitHub is down or
  rejects the request, that attempt is given back - an outage doesn't lock
  you out of reporting for an hour. (A reply GitHub sent but we couldn't
  read keeps its slot, since the issue may have been created.)

Past a cap, filing returns `429` until the window rolls over.

## What's *not* sent

Only the title and description you type, plus the version/timestamp
footer above (and your display name in Entra mode). No document content, no series data, no filesystem paths.
