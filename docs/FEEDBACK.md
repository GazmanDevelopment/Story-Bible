# Filing feedback from the pane

The Series tab's **Log Issue** / **Log Suggestion** buttons file a GitHub
issue directly against this repo, from inside Word (or the browser demo) -
no GitHub account needed for whoever's using the pane.

## How it works

The pane sends the title/description you type to `POST /api/feedback` (the
same `X-Token`/`STORYBIBLE_TOKEN` auth every other `/api/*` call already
needs - it isn't a new, separately-open endpoint). The **server** holds a
GitHub token and files the issue on your behalf:

- **Log Issue** → labelled `bug`, `area:pane`
- **Log Suggestion** → labelled `enhancement`, `area:pane`

A short footer is appended to the issue body noting it was filed from the
pane, the app version, and when. Nothing about who submitted it is
recorded yet - once Entra sign-in (#9/#10) lands, that'll change the same
way `created_by`/`updated_by` get added to records.

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

A small in-process limit (5 filings/hour by default) guards against an
accidental double-submit, not abuse from a stranger - this endpoint already
sits behind `STORYBIBLE_TOKEN`, and the deployment is LAN/VPN-only with a
couple of known users (see `SECURITY.md`). Past the cap, filing returns
`429` until the window rolls over.

## What's *not* sent

Only the title and description you type, plus the version/timestamp
footer above. No document content, no series data, no filesystem paths.
