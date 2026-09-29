# Security Policy

Story Bible is a small, self-hosted personal project (see [PLAN.md](PLAN.md)) -
there isn't a public multi-tenant service to protect, but the code is public
and self-hosters may run it, so vulnerability reports are still welcome and
taken seriously.

## Supported versions

There are no released versions yet - `main` is the only branch that gets
security fixes. Once the project reaches a tagged 1.0, this section will list
which versions still receive them.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.** Use GitHub's
private reporting instead, so the report isn't visible until a fix is out:

1. Go to the [Security tab](../../security) of this repo.
2. Click **Report a vulnerability**.

This opens a private advisory visible only to the maintainer and you, and
keeps the conversation off the public issue tracker.

This is a one-person hobby project maintained outside of work hours, not a
funded or staffed effort - there's no guaranteed response time, but reports
are read and taken seriously, and I'll acknowledge a report within a
few days.

## What's in scope

Bugs in this repository's code: authentication/authorization bypass, data
leakage between series or between users once multi-user sharing lands (see
`PLAN.md` §3), injection, and similar issues in `app/` or the task pane.

## What is reachable without signing in

Deliberately short, and enforced by `tests/test_public_surface.py` (a new
route that isn't on its allow-list fails the build unless it requires auth):

- `GET /api/health` - liveness for Docker/monitoring: `ok`, `auth` (whether
  auth is on), the version, and a `backup_ok` boolean.
- `GET /api/config` - the Entra tenant and client ids the task pane needs
  before it can sign in. Both are public values (they're in the manifest).
- `GET /` and the static task-pane files - the UI shell, no data.

The Swagger UI / ReDoc / `openapi.json` pages are **off by default**; setting
`ENABLE_API_DOCS=true` serves them without authentication, so only do that
on a machine you control.

## Response headers and request limits

Every response carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and a
`Content-Security-Policy` (no inline scripts, no `eval`, scripts only from the
app itself and Microsoft's Office.js host; `CONTENT_SECURITY_POLICY` overrides
it). Request bodies are capped while they stream (`MAX_BODY_BYTES`, and
`IMPORT_MAX_BODY_BYTES` for `/api/import`), so an oversized or chunked upload
is cut off at the cap rather than buffered.

Two things are deliberately **not** done in the app: `frame-ancestors` /
`X-Frame-Options` (Word Online and other Office hosts embed the pane from a
range of Microsoft origins, and a wrong list would blank it), and
`Strict-Transport-Security` (belongs on the TLS-terminating reverse proxy).

## What's already a known, accepted trade-off

A few things are deliberate design decisions written up in `PLAN.md`'s risk
table, not vulnerabilities:
- The intended deployment is LAN/VPN-only, behind a reverse proxy the
  operator controls - it is not meant to be exposed directly to the internet.
- Whoever administers the host can read the SQLite database directly; Entra
  ID sign-in controls what the *app* shows, not what a server admin with
  filesystem access can see.
- Traffic between a LAN-local reverse proxy and this service may be plain
  HTTP, by the operator's choice, on their own network.

If you think one of these is exploitable beyond what's described above (for
example, a way to reach the service or the database without the access those
points assume), that's still worth reporting.
