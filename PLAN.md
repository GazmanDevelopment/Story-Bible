# Story Bible: Word add-in plan

A Scrivener-style story bible (characters, places, relationships, timeline) that sits in a side panel in Word. The data lives on the TrueNAS box and is shared across every chapter document in a series. Users sign in with **Microsoft Entra ID**, so more than one person (you and your wife) can use it, each with their own series and optional sharing.

Status: **working demo** in this folder. The real build happens in VS Code, using this plan and the demo code as the starting point.

---

## 1. What the demo already does

| Area | Demo behaviour |
|---|---|
| Series | Multiple series (e.g. Series A, Series B), switched from a drop-down. Everything is scoped to a series. |
| Chapters | Each series has a numbered chapter list. Each chapter is its own Word doc. |
| Doc linking | "Link to this series" stores a tiny `{series_id, chapter_id}` tag inside the .docx. Opening that doc auto-selects its series and filters the timeline to its chapter. **No story content is stored in the document.** |
| Characters | Name, role, aliases, age at start, height, gender, hair, eyes, style, then **per-series default physical fields** (editable list), plus one-off fields per character, preferences, backstory, notes. |
| Relationships | Directional links ("Betsy *mistress of* Mark") with notes. Shown on both characters. |
| Places | Name, where, description, relevance, linked characters. |
| Timeline | Anchor is either a real date or a label ("Night one"). Events are stored as **+years/months/days offsets**, negative for backstory. The pane shows the computed date and **each character's age at that event**. Filter by chapter or character. |
| Research | Freeform notes (title, rich-text body, date entered) linkable to a chapter and any number of characters/places/timeline events. Body is edited with a basic WYSIWYG editor (Quill); pasted/inserted images are resized and compressed client-side before saving. Searchable and sortable (newest/oldest/title). |
| Word integration | **Find**: select a name in the doc, and the pane opens that character or place (aliases work). **Insert name at cursor.** |
| Backup | Export a series as JSON, and import it back. |
| Also works | The same page runs in a normal browser tab, which is handy for planning away from Word. |

What the demo does **not** do: user sign-in (only an optional shared token; Entra ID is planned in §3), per-user ownership or sharing, handling of simultaneous edits, offline mode, scene bookmarks, reference images on characters/places (Research entries can embed images, but nothing else can yet - see Phase 5), a relationship graph, or production packaging.

---

## 2. Architecture

```
                         Microsoft Entra ID (login.microsoftonline.com)
                           ▲ 1. sign in / get token      ▲ 3. fetch signing keys (JWKS)
                           │   (MSAL, nested app auth)    │    (cached; outbound only)
 Word (Windows / Mac)      │                              │   TrueNAS SCALE
 ┌────────────────────────┐│  2. HTTPS + Bearer token ┌───┴──────────────────────────┐
 │ Task pane (HTML/JS)    │┴─────────────────────────▶│ Reverse proxy (Caddy/Traefik)│
 │  MSAL.js (NAA)         │                           │  storybible.huscroft.com.au  │
 │  Office.js:            │                           └──────────────┬───────────────┘
 │   - selection lookup   │                                          │
 │   - insert text        │                           ┌──────────────▼───────────────┐
 │   - doc settings tag   │                           │ Story Bible container        │
 └────────────────────────┘                           │  FastAPI  (/api/*)           │
          ▲                                           │   - validates Entra JWT      │
          │ manifest.dev/prod.xml (sideloaded         │   - owner/member checks      │
          │ from an SMB share)                        │  serves task pane (/)        │
                                                      │  SQLite  → /data (dataset)   │
                                                      └──────────────────────────────┘
                                                                     ▲
                                              Review pipeline daemon (app-only token,
                                              client credentials) reads /api/.../bundle
```

The server only needs **outbound** access to Microsoft to fetch signing keys. Users get their tokens directly from Microsoft, so Entra sign-in works with the server kept LAN/VPN-only.

- **One container** serves both the API and the task pane files, so there are no CORS issues and only one thing to deploy.
- **SQLite on a TrueNAS dataset.** It's a single file, backed up by snapshots, and more than enough for one writer.
- **Records are stored as JSON documents** (`records` table: `id, series_id, kind, data, updated`). Adding a field in the UI never needs a database migration. The trade-off is weaker validation, which is acceptable for a single-user tool.

### Data model (logical)

```
User   { oid (Entra object id, PK), email, display_name, first_seen }   (auto-created on first sign-in)

Series { name, description, anchor_mode: date|relative, anchor_date, anchor_label,
         character_fields: [..],
         owner_oid, members: [{oid, role: editor|viewer}] }
 every record also gets: created_by, updated_by (oid), version (int, for concurrency)
 ├─ Chapter      { number, title }
 ├─ Character    { name, role, aliases, age, height, gender, hair, eyes, style,
 │                 custom: {field: value}, preferences, backstory, notes }
 ├─ Relationship { from, type, to, note }            (directional)
 ├─ Location     { name, place, description, relevance, character_ids[] }
 ├─ Event        { title, off_y, off_m, off_d, chapter_id, location_id,
 │                 character_ids[], description }
 └─ Research     { title, body (sanitized HTML), date_entered, chapter_id,
                   character_ids[], location_ids[], event_ids[] }            (#43)
```

### Key decisions (and why)

1. **Store offsets, not dates.** Moving the anchor shifts the whole 5-year timeline with no edits. Month arithmetic clamps to month-end (31 Jan + 1 month = 28/29 Feb), and leap years are handled.
2. **Age is stored as "age at start", not a birthdate.** It's simpler to write with, and age at any event = age + full years elapsed. Add an optional birthdate later if exact birthdays matter.
3. **The doc tag is only a pair of IDs.** Chapters sent to beta readers or publishers carry nothing sensitive. Clearing the tag isn't needed for privacy, but it's easy to add.
4. **Private sideload only.** AppSource content rules make store publishing a non-starter, and that isn't needed anyway.
5. **Optimistic concurrency, not last write wins.** With two people, one of you can silently overwrite the other's edit. Every record carries a `version`. A save sends it back, and if someone else saved first the server returns **409**. The pane then shows "changed by X, reload?" instead of overwriting.

---

## 3. Authentication and sharing (Entra ID)

### Approach: Nested App Authentication (NAA) with MSAL.js
- NAA is Microsoft's current recommended way for Office add-ins to sign users in. MSAL.js asks **Word** for a token for the account already signed into Word, so most of the time there's no login prompt. If that fails (different account, consent needed), it falls back to a popup.
- The same code runs in a normal browser tab: `createNestablePublicClientApplication` falls back to the standard SPA flow outside Office.
- The legacy Office SSO route (`getAccessToken` + on-behalf-of) is **not needed**. We don't call Microsoft Graph, only our own API.
- **Don't put the login at the reverse proxy** (oauth2-proxy, Authentik, Cloudflare Access). Redirect-and-cookie logins break inside Word's embedded task pane. Keep the proxy for TLS only and do auth in the app with bearer tokens.

### App registration (in the horscrust.com tenant)

Two different domains are in play here, deliberately: the app itself is
served from **`storybible.huscroft.com.au`** (the redirect URIs below, the
reverse proxy, the manifest's URLs - see #6/#7/#8), but sign-in happens
against the **`horscrust.com`** Entra tenant regardless of what domain the
app is served from - a tenant authenticates users, it doesn't need to match
the domain hosting the app that redirects to it.

| Setting | Value |
|---|---|
| Name | Story Bible |
| Supported account types | **Single tenant** (see "Your wife's account" below) |
| Platform: Single-page application, redirect URIs | `brk-multihub://storybible.huscroft.com.au` (NAA), `https://storybible.huscroft.com.au` (browser), `https://localhost:3000` (dev) - the app's own domain, not the tenant's |
| Expose an API | App ID URI `api://<client-id>`, delegated scope **`access_as_user`** |
| App role (Application type) | **`Pipeline.Read`**, for the review-pipeline daemon (client credentials, separate app registration with a certificate or secret) |
| Enterprise app → Properties | **Assignment required = Yes**, then assign only you and your wife. Nobody else in the tenant can get a token. |

### Your wife's account (decide before registering)
- **A. She has, or gets, an account in the horscrust.com tenant.** This is the simplest option. If Word is signed in with a different account, MSAL shows a one-time popup to pick the horscrust account, then caches it.
- **B. She uses a personal Microsoft account (outlook.com / hotmail).** Invite that account as a **B2B guest** into the tenant, keep the app single-tenant, and set the MSAL authority to your tenant ID. "Assignment required" still works for guests.
- **Avoid** registering as "any organisation + personal accounts". Any Microsoft account on earth could then get a token, and the server allowlist would become the only gate.

### Server side (FastAPI)
- Validate every request's `Authorization: Bearer` JWT:
  - signature via Entra's JWKS (PyJWT `PyJWKClient`, cached)
  - `iss` = `https://login.microsoftonline.com/<tenant-id>/v2.0`
  - `aud` = the client ID
  - `exp`
  - and either `scp` contains `access_as_user` (a person) or `roles` contains `Pipeline.Read` (the daemon, read-only)
- `fastapi-azure-auth` does most of this if you'd rather not hand-roll the ~40 lines.
- Identify people by **`oid`** (plus `tid`), never by email. Create a `users` row on first sign-in.
- Defence in depth: an `ALLOWED_OIDS` env var. Anyone else gets a 403, even with a valid token.
- `AUTH_MODE` env: `none` (local demo), `token` (the current shared secret), `entra` (production).

### Ownership and sharing
- Each series has an **owner**. By default only the owner sees it, so your series and hers stay separate.
- **Share** from the Series tab: pick a known user (anyone who has signed in once) as **editor** or **viewer**. Records inherit their series' permissions. Only the owner can delete a series or change sharing.
- `created_by` / `updated_by` on every record. The pane shows "edited by … 2 days ago".
- Be upfront with her about one thing: whoever administers the TrueNAS box can read the SQLite file directly. Entra controls what the **app** shows, not what the server admin can see.

### Task pane changes
- Add `@azure/msal-browser` and get tokens with `acquireTokenSilent`, falling back to `acquireTokenPopup`. Refresh the token silently before each save.
- Add the signed-in name and a sign-out option to the header. Remove the "API token" box.
- Check `Office.context.requirements.isSetSupported("NestedAppAuth", "1.1")`. Current Microsoft 365 Word supports it. **Perpetual Office 2021/2024 may not**, in which case fall back to the Office dialog API (`displayDialogAsync`) for the popup. Add `https://login.microsoftonline.com` to the manifest's `<AppDomains>` for that path (already there in `manifest.prod.xml`, ahead of this landing - #8).
- Pulling in MSAL via npm is the point where a small **Vite build** pays for itself (see Phase 0).

---

## 4. Build phases (VS Code)

### Phase 0: project setup (½ day)
- Copy this folder into a git repo. Keep the backend layout (`app/main.py`).
- Front end: **move to Vite (plain JS or TypeScript)**, because Entra sign-in needs `@azure/msal-browser` from npm. Keep the demo's structure and let Vite bundle it into `app/static/`. Office's `yo office` generator also works, but it brings webpack and React by default, which is more than this needs.
- Install the VS Code extensions: *Python*, *Office Add-ins Development Kit*. Run `npx office-addin-dev-certs install` once.
- Debug loop: `python run_demo.py --https`, then sideload `manifest.dev.xml` into Word (see §6; generated from `manifest.template.xml` by `scripts/generate_manifest.py`, #8).

### Phase 1: harden what the demo does (1–2 days)
- Pydantic models per record kind (validation, defaults) while keeping the JSON storage.
- Proper error states in the pane (server down, 401, save failed) and an unsaved-changes guard on Back.
- Tests: move the demo's API test and headless-browser test (`tests/`) into CI or a pre-commit step.

### Phase 2: deploy to TrueNAS (½–1 day)
- `Dockerfile` (python:3.12-slim, `uvicorn app.main:app --host 0.0.0.0 --port 8000`).
- TrueNAS **Custom App** (compose), with a dataset such as `tank/apps/storybible` mounted at `/data`.
- Reverse proxy with a real cert (Let's Encrypt DNS challenge), e.g. `storybible.huscroft.com.au`.
- **Still recommend LAN/VPN-only**, even with Entra. Sign-in doesn't need the server to be public (see §2). If your wife uses it away from home, Tailscale on her laptop is simpler than exposing the server.
- Update the manifest URLs, then sideload from an SMB share (see §6). Her PC needs the same Trusted Add-in Catalog setting.
- Snapshot task on the dataset, plus a nightly JSON export per series.

### Phase 3: Entra sign-in and sharing (2–3 days)
- App registration, guest invite if needed, and assignment (see §3).
- Server: JWT validation, `users` table, `AUTH_MODE`, owner/member checks on every route, `version` + 409 on updates, `created_by`/`updated_by`.
- Pane: MSAL (NAA), header user badge, Share panel in the Series tab, and a conflict prompt on 409.
- Migration: the first user to sign in claims any existing (pre-auth) series as owner.
- Tests: fake-token fixture (sign test JWTs with a local key and point the validator at it). Cover a non-member getting 403, a viewer being unable to write, and a stale version getting 409.

### Phase 4: deeper Word integration (2–4 days)
- **Scene anchors:** "Mark this scene" wraps the selection in a content control or bookmark and stores `{chapter_id, bookmark}` on an event, so clicking the event jumps to the scene (same doc) or tells you which chapter to open.
- **Highlight known names** in the current doc (search for each character name and alias, with a temporary highlight toggle).
- **"Who's in this chapter?"**: scan the doc for names and alias hits, then list them with counts.

### Phase 5: story tools (pick and choose)
- **Relationship map** (small force-directed graph, e.g. Cytoscape.js).
- **Events relative to other events** ("3 days after the party").
- **Reference images** per character or place (stored under `/data/media`).
- **Per-chapter state**: hair, relationship status and so on can change over 5 years. Add optional "from chapter N" overrides on character fields.
- Full-text search across the whole series.

### Phase 6: connect to the review pipeline
- The pipeline already has a "character DB continuity checker" on the back burner. **This service can be that database.** The checker gets an app-only token (client credentials, `Pipeline.Read` role, read-only) and calls `GET /api/series/{id}/bundle` and gives the local LLM the character sheet for the chapter being reviewed. For example, it could flag "Betsy's eyes are *green* in the bible, but the text says *blue*" as a Word comment.
- The doc tag (`series_id`, `chapter_id`) is readable from the .docx (`word/webextensions/`), so the pipeline knows which bible to load without any extra setup.

---

## 5. Risks and open questions

| Risk | Mitigation |
|---|---|
| Server unreachable, so the pane is empty | Phase 1 error state. Later: cache the last bundle in `localStorage` and show it read-only. |
| Word caches old pane files after updates | Version the static files (`app.js?v=`) or clear `%LOCALAPPDATA%\Microsoft\Office\16.0\Wef\`. |
| Office requires trusted HTTPS | Dev: Office dev cert. Prod: real cert via the reverse proxy. |
| Sensitive data exposure | LAN/VPN only, Entra sign-in with assignment required, an `ALLOWED_OIDS` allowlist, and no content in the .docx. |
| Word signed in with a different account than the one allowed | One-time MSAL popup to choose the right account, cached after that. |
| Older/perpetual Word without NAA | Detect with `isSetSupported("NestedAppAuth","1.1")` and fall back to the Office dialog API. |
| Popups blocked in the task pane | Always start `acquireTokenPopup` from a button click (user gesture). |
| Two people editing the same record | `version` + 409 conflict prompt (decision 5). |
| Server admin can read everything | By design. Tell your wife; if that matters, encrypt per-user fields (not planned). |
| Characters appearing in both Series A and B | Not supported (series-scoped). If it comes up, add a "copy character to series" action rather than sharing records. |

---

## 6. Loading the add-in into Word

**Windows (recommended: shared-folder catalog)**
1. Put `manifest.dev.xml` (or `manifest.prod.xml` once deployed - see #6/#7) in a folder that's shared over SMB, e.g. `\\truenas\addins` or even a local shared folder.
2. Word → File → Options → Trust Center → Trust Center Settings → **Trusted Add-in Catalogs**. Add the UNC path, tick **Show in Menu**, then OK.
3. Restart Word → Home → **Add-ins → More add-ins → Shared Folder → Story Bible**. After that it shows as a button on the Home tab.

**Mac:** copy `manifest.dev.xml` (or `manifest.prod.xml`) to `~/Library/Containers/com.microsoft.Word/Data/Documents/wef/`, then restart Word.

---

## 7. Running the demo

```bash
pip install fastapi uvicorn
python run_demo.py                 # http://localhost:8765 in a browser, seeded with two sample series
npx office-addin-dev-certs install # once, for Word
python run_demo.py --https         # https://localhost:3000, then sideload manifest.dev.xml
```

The sample data (`samples/*.json`) is fictional, with "(your detail here)" placeholders for the explicit fields. Import your own via Series tab → Import JSON.
