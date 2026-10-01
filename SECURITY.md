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
- `GET /api/config` - the Entra tenant and client ids, the sign-in authority
  and the optional privacy/terms links the task pane needs
  before it can sign in. All are public values (the ids are in the manifest).
- `GET /` and the static task-pane files - the UI shell, no data.

The Swagger UI / ReDoc / `openapi.json` pages are **off by default**; setting
`ENABLE_API_DOCS=true` serves them without authentication, so only do that
on a machine you control.

## Response headers and request limits

Every response carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, a restrictive `Permissions-Policy`, and a
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

Also accepted, each with the reasoning (all added in the #44 audit, #74):
- **Backups sit in the same dataset as the database, in plaintext.** They are
  created owner-only (`0700`/`0600`), which protects against other accounts on
  the host, not against the host's administrator or against losing the pool.
  Protecting against pool loss means copying them off the box - see
  [docs/BACKUP.md](docs/BACKUP.md).
- **`office.js` is loaded from Microsoft's CDN without a Subresource Integrity
  hash.** Microsoft only supports the floating `/lib/1/hosted/` URL (it is
  updated in place), so a fixed hash would break the pane whenever they
  publish. The Content-Security-Policy limits *where* scripts may load from
  (this host and the app itself). A compromise of that host would run in the
  pane, with access to whatever the pane can reach.
- **Sign-in tokens are kept in the browser's `localStorage`** (the MSAL cache,
  chosen deliberately so the pane stays signed in when Word closes and
  reopens it; and the legacy shared token, `sb_token`). Script running in the
  pane could read them. That is why there is no inline script, no `eval`, all
  rich text is sanitized server-side on write, and the CSP restricts script
  sources - the exposure is the pane's own XSS surface, kept small on purpose.
- **A reviewed advisory in a vendored library:** Quill 2.0.3 has CVE-2025-15056
  (XSS in its HTML *export* feature; no fixed release exists). The pane does not
  use that feature, and a test fails if it starts to. Vendored JavaScript is
  re-checked weekly against the npm advisory database
  (`scripts/check_vendored_js.py`, see `.github/workflows/security-audit.yml`).
- **The plain-HTTP port (2285) is reachable from the LAN** unless you restrict it;
  [deploy/README.md](deploy/README.md) ("Network exposure") says how.

If you think one of these is exploitable beyond what's described above (for
example, a way to reach the service or the database without the access those
points assume), that's still worth reporting.
