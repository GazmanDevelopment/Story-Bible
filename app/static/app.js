/* Story Bible task pane - demo build.
 * Plain JS, no build step. Works inside Word (Office.js) and in a normal browser;
 * everything document-specific goes through `host` (host.js).
 */
"use strict";

const S = {
  seriesList: [],
  sid: null,          // current series id
  b: null,            // bundle for current series
  tab: "characters",
  view: null,         // {kind, id} when editing a record, else null (list)
  filter: "",
  tlChapter: "", tlChars: [], tlLocs: [],
  rsSort: "date_desc",  // research list: date_desc | date_asc | title
  docLink: null,      // {series_id, chapter_id} stored in the Word document
  me: null,           // {isAdmin, ...} from GET /api/me - only fetched in entra mode
  adminError: null,
  adminUsers: null,   // admin view's user list
  config: null,       // {authMode, tenantId, clientId, authority, privacyUrl, termsUrl} from GET /api/config (#13, #91)
  formSnapshot: null, // JSON of the open form's fields right after rendering - unsaved-changes guard (#58)
  members: null,      // {owner_oid, owner_display_name, owner_email, members: [...]} for the open series (#148)
};

// MSAL state (#13) - not on S since it holds live library objects, not
// plain data; msalAccount is the only piece the UI needs to read.
let msalPca = null;
let msalAccount = null;

// Help guide on GitHub Pages (#81).
const HELP_URL = "https://gazmandevelopment.github.io/Story-Bible/help/";

const REL_TYPES = ["married to", "partner of", "mistress of", "lover of", "ex of",
  "friend of", "best friend of", "sibling of", "parent of", "boss of", "colleague of",
  "neighbour of", "rival of", "flirts with"];

const DEFAULT_FIELDS = ["Build", "Skin", "Tattoos", "Piercings",
  "Distinguishing marks", "Voice & mannerisms"];

// ------------------------------------------------------------------ helpers
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const byName = (a, b) => (a.name || a.title || "").localeCompare(b.name || b.title || "");
function lsGet(k, d = "") { try { return localStorage.getItem(k) ?? d; } catch { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

function toast(msg, type = "info") {
  const t = $("#toast"); t.textContent = msg;
  t.className = type === "error" ? "show error" : "show";
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 1800);
}

// #58: every thrown error from here carries `.status` (when it's an HTTP
// response, not a network failure) and, for a 409, `.detail` (the raw
// conflict payload - {error, current, updated_by} - so a caller can build
// a real reload/keep-mine prompt instead of just reading the message).
// #72: `opts.ifNoneMatch` sends a conditional GET and a 304 resolves to
// NOT_MODIFIED; `opts.etagOut` (an object) receives the response's ETag.
// (The browser's own HTTP cache can't do this for us: /api/* is no-store.)
const NOT_MODIFIED = Symbol("not modified");
async function api(path, method = "GET", body, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.ifNoneMatch) headers["If-None-Match"] = opts.ifNoneMatch;
  if (S.config?.authMode === "entra") {
    const token = await getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } else {
    const tok = lsGet("sb_token"); if (tok) headers["X-Token"] = tok;
  }
  let res;
  try {
    res = await fetch("/api" + path, { method, headers,
      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    // fetch() itself throws (offline, DNS, connection refused) rather than
    // resolving with a response - a raw "Failed to fetch" isn't useful.
    throw new Error("Can't reach the server - check your connection and try again");
  }
  if (res.status === 304) return NOT_MODIFIED;
  if (opts.etagOut) opts.etagOut.etag = res.headers.get("ETag");
  if (!res.ok) {
    if (res.status === 401) {
      // A stale/expired token in entra mode: the header's cached account
      // no longer gets one, so drop it and let renderHeader() show Sign in
      // again immediately rather than leaving no way back in but a reload.
      if (S.config?.authMode === "entra") { msalAccount = null; renderHeader(); }
      const err = new Error(S.config?.authMode === "entra" ? "Signed out - sign in again" : "Needs API token (Series tab → Connection)");
      err.status = 401;
      throw err;
    }
    const txt = await res.text();
    // #12/#58: 409/428 (and 400s from Pydantic) carry a structured `detail`
    // rather than a plain string - surface something readable instead of
    // the raw JSON blob.
    let message = txt;
    let detail;
    try {
      detail = JSON.parse(txt).detail;
      if (typeof detail === "string") {
        message = res.status === 403 ? `You don't have access: ${detail}` : detail;
      } else if (res.status === 409 && detail?.error === "conflict") {
        message = `Changed by ${detail.updated_by || "someone else"} since you loaded it`;
      } else if (res.status === 403 && detail?.error === "policy_not_accepted") {
        // The server enforces acceptance too (the policy changed since this pane
        // loaded, say): bring the acceptance screen back rather than a raw error.
        message = "Please accept the privacy policy and terms to continue";
        S.me = { ...(S.me || {}), policyCurrent: false }; S.view = null; render();
      }
    } catch { /* not JSON - fall back to the raw text above */ }
    const err = new Error(message);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.json();
}

// ----------------------------------------------------------- confirm modal
// Resolves true for the primary ("confirm") button, false for Cancel.
let _modalResolve = null;
function showModal(message, confirmLabel) {
  return new Promise((resolve) => {
    _modalResolve?.(false); // an unresolved prior modal would otherwise hang forever
    _modalResolve = resolve;
    $("#modalMessage").textContent = message;
    $("#modalConfirmBtn").textContent = confirmLabel;
    $("#modalOverlay").hidden = false;
  });
}
function closeModal(result) {
  $("#modalOverlay").hidden = true;
  const resolve = _modalResolve; _modalResolve = null;
  resolve?.(result);
}
const rec = (kind, id) => (S.b?.[kind] || []).find((r) => r.id === id);
const charName = (id) => rec("characters", id)?.name || "?";

// ------------------------------------------------------------- timeline math
function addOffset(iso, y, m, d) {
  const [Y, M, D] = iso.split("-").map(Number);
  const tm = (M - 1) + y * 12 + m;
  const ty = Y + Math.floor(tm / 12), tmo = ((tm % 12) + 12) % 12;
  const dim = new Date(Date.UTC(ty, tmo + 1, 0)).getUTCDate();
  const dt = new Date(Date.UTC(ty, tmo, Math.min(D, dim)));
  dt.setUTCDate(dt.getUTCDate() + d);
  return dt;
}
function off(e) { return [Number(e.off_y) || 0, Number(e.off_m) || 0, Number(e.off_d) || 0]; }
function sortKey(e) {
  const [y, m, d] = off(e); const s = S.b.series;
  if (s.anchor_mode === "date" && s.anchor_date) return addOffset(s.anchor_date, y, m, d).getTime();
  return y * 365.25 + m * 30.44 + d;
}
function yearsElapsed(e) {
  const [y, m, d] = off(e); const s = S.b.series;
  if (s.anchor_mode === "date" && s.anchor_date) {
    const a = new Date(s.anchor_date + "T00:00:00Z"), b = addOffset(s.anchor_date, y, m, d);
    let yrs = b.getUTCFullYear() - a.getUTCFullYear();
    if (b.getUTCMonth() < a.getUTCMonth() ||
        (b.getUTCMonth() === a.getUTCMonth() && b.getUTCDate() < a.getUTCDate())) yrs--;
    return yrs;
  }
  return Math.floor((y * 365.25 + m * 30.44 + d) / 365.25);
}
function relLabel(e) {
  const [y, m, d] = off(e);
  if (!y && !m && !d) return S.b.series.anchor_label || "Story start";
  const parts = [];
  if (y) parts.push(`${y}y`); if (m) parts.push(`${m}m`); if (d) parts.push(`${d}d`);
  const neg = sortKey(e) < sortKey({});
  return (neg ? "" : "+") + parts.join(" ").replace(/-/g, "−");
}
function whenLabel(e) {
  const s = S.b.series, r = relLabel(e);
  if (s.anchor_mode === "date" && s.anchor_date) {
    const dt = addOffset(s.anchor_date, ...off(e));
    return dt.toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })
      + ` · ${r}`;
  }
  return r;
}
function ageAt(c, e) {
  const a = parseInt(c.age, 10);
  return Number.isFinite(a) ? a + yearsElapsed(e) : null;
}

// -------------------------------------------------------- auth (#13, MSAL)
// createNestablePublicClientApplication (vendored MSAL, see
// app/static/vendor/msal/NOTICE.md) is Microsoft's recommended single
// entry point for both cases at once: inside Word when nested app auth is
// supported, it does NAA; outside Word (or when it isn't) it behaves like
// a normal PublicClientApplication, i.e. the standard browser-tab SPA
// flow. Only genuinely old Word *without* NAA needs the separate dialog
// fallback below.
function msalScope() { return `api://${S.config.clientId}/access_as_user`; }

async function initAuth() {
  try {
    S.config = await (await fetch("/api/config")).json();
  } catch {
    S.config = { authMode: "none", tenantId: "", clientId: "", authority: "", privacyUrl: "", termsUrl: "", policyVersion: "", buildVersion: "", buildDate: "" };  // server unreachable - boot()'s own error state takes it from here
  }
  if (S.config.authMode !== "entra") return;
  const msalConfig = {
    // authority comes from the server (#91): the home tenant, or "common" in open
    // signup. The tenantId fallback is for a server that predates the field.
    auth: { clientId: S.config.clientId, authority: S.config.authority || `https://login.microsoftonline.com/${S.config.tenantId}` },
    cache: { cacheLocation: "localStorage" },  // survives the task pane closing/reopening with a document
  };
  try {
    if (host.nestedAuth()) {
      msalPca = await msal.createNestablePublicClientApplication(msalConfig);
    } else {
      msalPca = new msal.PublicClientApplication(msalConfig);
      await msalPca.initialize();
    }
    const accounts = msalPca.getAllAccounts();
    if (accounts.length) msalAccount = accounts[0];
  } catch (err) {
    console.error("MSAL init failed", err);
  }
}

async function getAuthToken() {
  if (!msalPca || !msalAccount) return null;
  try {
    const result = await msalPca.acquireTokenSilent({ scopes: [msalScope()], account: msalAccount });
    return result.accessToken;
  } catch (err) {
    console.error("Silent token acquisition failed", err);
    return null;  // the next API call 401s, surfacing "sign in again" rather than looping silently
  }
}

async function signIn() {
  if (!msalPca) return;
  try {
    if (host.nestedAuth()) {
      const result = await msalPca.acquireTokenPopup({ scopes: [msalScope()] });
      msalAccount = result.account;
    } else {
      // Old Word without NAA: the interactive part runs in a separate dialog
      // (see host.js); this instance then just re-reads the account both
      // pages share via the same localStorage cache.
      await host.openAuthDialog();
      msalAccount = msalPca.getAllAccounts()[0] || null;
    }
  } catch (err) {
    toast("Sign-in failed: " + err.message, "error");
  }
}

async function signOut() {
  if (msalPca && msalAccount) {
    try { await msalPca.getTokenCache().removeAccount(msalAccount); } catch { /* best-effort */ }
  }
  msalAccount = null;
}

// ------------------------------------------------------------- document link
// The document itself (Word, ...) is reached only through `host` (host.js).
async function readDocLink() {
  if (!host.hasDocument) return;
  S.docLink = await host.readDocLink();
}
function saveDocLink(link) {
  S.docLink = link;
  host.saveDocLink(link).then(() => toast("Document linked"), (err) => toast(err.message, "error"));
}

// ------------------------------------------------------------------- loading
async function loadMe() {
  // Only to know whether to show the Admin button; the server enforces it.
  S.me = null;
  if (S.config?.authMode !== "entra") return;
  try { S.me = await api("/me"); } catch { /* not fatal: no admin button */ }
}
async function loadSeriesList() {
  // summary: just id/name/version - the picker needs nothing else, and a
  // series' full settings can be very large (#72).
  S.seriesList = await api("/series?summary=true");
}
// Bumped whenever S.b is replaced or patched, so a background request that
// was already in flight can tell its answer is out of date and drop it (#72).
let bundleGen = 0;
async function loadBundle() {
  if (!S.sid) { S.b = null; S.bEtag = null; bundleGen++; return; }
  const out = {};
  const r = await api(`/series/${S.sid}/bundle`, "GET", undefined, { etagOut: out });
  S.b = r; S.bEtag = out.etag || null; bundleGen++;
}
// #72: after a save/add the server's response is the record itself, so patch
// local state from it instead of re-downloading the whole bundle. (Deletes
// still reload: the server cascades into other records that reference the
// deleted one.) The bundle's ETag no longer matches once we've patched.
function putLocal(kind, record) {
  const list = S.b[kind], i = list.findIndex((x) => x.id === record.id);
  if (i >= 0) list[i] = record; else list.push(record);
  S.bEtag = null; bundleGen++;
}
// Pick up other people's changes without a reload: on window focus, tab
// switch or coming back to the pane. Only ever when no form is open - a
// form holds the version it was loaded at, and swapping the data under it
// would let a save silently overwrite someone else's edit (defeating #12).
let lastRevalidate = 0;
const formOpen = () => !!(S.view || $("#main form") || !$("#modalOverlay").hidden);
async function revalidateBundle() {
  if (!S.sid || !S.b || formOpen()) return;
  if (Date.now() - lastRevalidate < 15000) return;
  lastRevalidate = Date.now();
  const sid = S.sid, gen = bundleGen, out = {};
  try {
    const r = await api(`/series/${sid}/bundle`, "GET", undefined, { ifNoneMatch: S.bEtag || undefined, etagOut: out });
    // Re-check AFTER the round trip, before touching any state: in that time
    // the user may have opened a form (a save from it would then carry the
    // refreshed version with stale field values and overwrite someone's edit
    // without a 409), saved something (this answer predates it), or switched
    // series. Any of those means this response is discarded.
    if (r === NOT_MODIFIED || S.sid !== sid || bundleGen !== gen || formOpen()) return;
    S.b = r; S.bEtag = out.etag || null; bundleGen++;
    render();
  } catch { /* a background refresh: stay quiet, the next real action will surface any error */ }
}
async function loadMembers() {
  // #148: sharing only has real per-user identity in entra mode - none/
  // token modes keep today's single-shared-bible behaviour, so there's
  // nothing meaningful to show.
  S.members = S.sid && S.config?.authMode === "entra" ? await api(`/series/${S.sid}/members`) : null;
}
async function selectSeries(id) {
  if ((id || null) !== S.sid) { S.tlChars = []; S.tlLocs = []; }  // pills belong to one series
  S.sid = id || null; S.view = null; lsSet("sb_series", S.sid || "");
  await Promise.all([loadBundle(), loadMembers()]); render();  // independent requests (#148)
}

// ------------------------------------------------------------------ renderers
function renderHeader() {
  const sel = $("#seriesSelect");
  sel.innerHTML = (S.seriesList.length ? "" : `<option value="">No series yet</option>`) +
    S.seriesList.map((s) => `<option value="${s.id}" ${s.id === S.sid ? "selected" : ""}>${esc(s.name)}</option>`).join("") +
    `<option value="__new">+ New series…</option>`;
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === S.tab));

  const ab = $("#authBar");
  ab.innerHTML = S.config?.authMode !== "entra" ? "" : (msalAccount
    ? `<span>${esc(msalAccount.name || msalAccount.username || "Signed in")}</span>${S.me?.isAdmin ? `<button class="small" data-act="open-admin">Admin</button>` : ""}<button class="small" data-act="open-account">Account</button><button class="small" data-act="sign-out">Sign out</button>`
    : `<button class="small primary" data-act="sign-in">Sign in</button>`);

  const lb = $("#linkBar");
  if (!host.hasDocument || !S.b) { lb.innerHTML = ""; return; }
  const linked = S.docLink && S.docLink.series_id === S.sid;
  const chOpts = `<option value="">(no chapter)</option>` + [...S.b.chapters]
    .sort((a, b) => (a.number || 0) - (b.number || 0))
    .map((c) => `<option value="${c.id}" ${linked && S.docLink.chapter_id === c.id ? "selected" : ""}>
      Ch ${esc(c.number)} – ${esc(c.title)}</option>`).join("");
  lb.innerHTML = linked
    ? `This doc: <select data-act="doc-chapter">${chOpts}</select>`
    : `<span>This document isn't linked.</span><button class="small" data-act="doc-link">Link to this series</button>`;
}

// Signed in but not yet accepted the current privacy policy and terms (#111):
// the pane shows only the acceptance screen until they do.
function policyGateNeeded() {
  return S.config?.authMode === "entra" && !!S.me && S.me.policyCurrent === false;
}

function policyGateHtml() {
  return `<div class="empty policy-gate"><strong>Before you continue</strong><br><br>
    By continuing you accept the terms and the privacy policy.${legalLinksHtml()}<br>
    <button class="primary" data-act="accept-policy">Accept and continue</button></div>`;
}

function render() {
  renderHeader();
  const m = $("#main");
  if (policyGateNeeded()) { m.innerHTML = policyGateHtml(); S.formSnapshot = null; return; }
  // Checked before the "no series" empty state below, not after: feedback
  // doesn't need a series to exist (it isn't tied to S.b at all), and if
  // it were gated behind having one, a user hitting a bug that prevents
  // creating their first series could never report that exact bug.
  if (S.view?.kind === "feedback") { m.innerHTML = renderForm(); captureFormSnapshot(); return; }
  if (S.view?.kind === "account") { m.innerHTML = `<button class="link back" data-act="cancel">← Back</button>` + accountView(); S.formSnapshot = null; return; }
  if (S.view?.kind === "admin") { m.innerHTML = `<button class="link back" data-act="cancel">← Back</button>` + adminView(); S.formSnapshot = null; return; }
  if (!S.b) {
    S.formSnapshot = null;
    m.innerHTML = `<div class="empty">Create a series to get started.<br><br>
      <button class="primary" data-act="new-series">+ New series</button>
      <div class="toolbar" style="margin-top:12px;justify-content:center">
        <button type="button" data-act="log-issue">Log Issue</button>
        <button type="button" data-act="log-suggestion">Log Suggestion</button>
      </div></div>`;
    return;
  }
  if (S.view) {
    m.innerHTML = renderForm();
    if (S.view.kind === "research") initResearchEditor(S.view.id ? rec("research", S.view.id) : {});
    captureFormSnapshot();
    return;
  }
  m.innerHTML = ({ characters: listCharacters, locations: listLocations,
    events: listEvents, research: listResearch, series: seriesForm })[S.tab]();
  captureFormSnapshot();
}

// #58: unsaved-changes guard. Snapshotting the form right after it's
// rendered (rather than diffing against the server record) naturally
// covers fields the form pre-fills with defaults a new record doesn't
// have yet (e.g. Research's date_entered defaults to today) - only what
// the user actually changes afterwards counts as dirty.
function captureFormSnapshot() {
  const form = $("main form");
  S.formSnapshot = form ? JSON.stringify(readForm(form)) : null;
}
function isDirty() {
  const form = $("main form");
  if (!form || S.formSnapshot == null) return false;
  return JSON.stringify(readForm(form)) !== S.formSnapshot;
}
// #127: actions inside an open form that change something *else* (add/remove a
// relationship, add/remove a chapter) re-render the form from saved data, which
// threw away anything typed but not yet saved - and re-baselined the snapshot, so
// the unsaved-changes guard stayed quiet too. This puts the typed values back
// into the freshly rendered form and keeps the original snapshot, so it still
// counts as dirty. Forms with a rich-text editor (Research) don't use it.
function customFieldHtml(name, value = "") {
  return `<span class="hint" style="margin:6px 0 0">${esc(name)}</span><input data-custom="${esc(name)}" value="${esc(value)}"><span></span>`;
}
function rerenderKeepingEdits() {
  const form = $("main form");
  const typed = form ? readForm(form) : null, snapshot = S.formSnapshot;
  render();
  const next = $("main form");
  if (!typed || !next || next.dataset.kind !== form.dataset.kind) return;
  next.querySelectorAll("[data-f]").forEach((el) => {
    const v = typed[el.dataset.f];
    if (v === undefined) return;
    el.value = Array.isArray(v) ? v.join("\n") : v;  // series list fields are textareas, one per line
  });
  next.querySelectorAll("[data-chips]").forEach((el) => {
    const on = typed[el.dataset.chips] || [];
    el.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", on.includes(c.dataset.id)));
  });
  const box = next.querySelector("#customFields");
  // readForm leaves emptied custom fields out of `typed`, so an input with no entry
  // there was cleared by the user and must not pick its saved value back up.
  const typedCustom = typed.custom || {};
  next.querySelectorAll("[data-custom]").forEach((el) => { el.value = typedCustom[el.dataset.custom] ?? ""; });
  Object.entries(typedCustom).forEach(([k, v]) => {
    if (![...next.querySelectorAll("[data-custom]")].some((el) => el.dataset.custom === k)) {
      box?.insertAdjacentHTML("beforeend", customFieldHtml(k, v));
    }
  });
  S.formSnapshot = snapshot;
}
async function confirmDiscard() {
  if (!isDirty()) return true;
  return showModal("You have unsaved changes. Discard them?", "Discard");
}

function toolbar(kind, placeholder, extra = "") {
  return `<div class="toolbar"><input data-act="filter" placeholder="${placeholder}" value="${esc(S.filter)}">
    ${extra}<button class="primary" data-act="new" data-kind="${kind}">+ Add</button></div>`;
}
// #129: the list search looks at the text a person can see, per kind - not the
// record's JSON, which also holds field names ("role", "notes"), ids, audit
// fields and a research body's tags and base64 image data.
const SEARCH_FIELDS = {
  characters: ["name", "role", "aliases", "age", "height", "gender", "hair", "eyes", "style",
    "preferences", "backstory", "notes"],
  locations: ["name", "place", "description", "relevance", "notes"],
  events: ["title", "description"],
  research: ["title", "date_entered"],
};
const searchCache = new WeakMap();  // record object -> lower-cased text; records are replaced, never edited in place
function searchText(kind, r) {
  let t = searchCache.get(r);
  if (t === undefined) {
    const parts = SEARCH_FIELDS[kind].map((f) => r[f]);
    if (kind === "characters") parts.push(...Object.values(r.custom || {}));
    if (kind === "research") parts.push(htmlText(r.body));
    t = parts.filter(Boolean).join("\n").toLowerCase();
    searchCache.set(r, t);
  }
  return t;
}
function matches(kind, r) {
  if (!S.filter) return true;
  return searchText(kind, r).includes(S.filter.toLowerCase());
}

function listCharacters() {
  const items = S.b.characters.filter((r) => matches("characters", r)).sort(byName);
  return toolbar("characters", "Search characters…") + (items.length
    ? `<ul class="list">${items.map((c) => `<li data-act="open" data-kind="characters" data-id="${c.id}">
        <div class="title">${esc(c.name)} ${c.role ? `<span class="sub">· ${esc(c.role)}</span>` : ""}</div>
        <div class="sub">${esc([c.age && c.age + " yo", c.hair, c.eyes, c.style].filter(Boolean).join(" · "))}</div>
        <div class="sub">${relsFor(c.id).slice(0, 3).map((r) => esc(relSentence(r, c.id))).join("; ")}</div>
      </li>`).join("")}</ul>`
    : `<div class="empty">No characters yet.</div>`);
}

function listLocations() {
  const items = S.b.locations.filter((r) => matches("locations", r)).sort(byName);
  return toolbar("locations", "Search places…") + (items.length
    ? `<ul class="list">${items.map((l) => `<li data-act="open" data-kind="locations" data-id="${l.id}">
        <div class="title">${esc(l.name)}</div>
        <div class="sub">${esc(l.place || "")}</div>
        <div class="sub">${(l.character_ids || []).map(charName).map(esc).join(", ")}</div>
      </li>`).join("")}</ul>`
    : `<div class="empty">No places yet.</div>`);
}

function listEvents() {
  const s = S.b.series;
  const chOpts = `<option value="">All chapters</option>` + S.b.chapters
    .sort((a, b) => (a.number || 0) - (b.number || 0))
    .map((c) => `<option value="${c.id}" ${S.tlChapter === c.id ? "selected" : ""}>Ch ${esc(c.number)}</option>`).join("");
  // pill filters highlight rather than hide; ignore ids deleted since they were picked
  const chars = S.tlChars.filter((id) => rec("characters", id)), locs = S.tlLocs.filter((id) => rec("locations", id));
  const pills = (label, kind, all, on) => all.length
    ? `<div class="tl-pills"><span class="lbl">${label}</span><div class="chips">${all.map((x) =>
      `<span class="chip ${on.includes(x.id) ? "on" : ""}" data-act="tl-chip" data-kind="${kind}" data-id="${x.id}">${esc(x.name)}</span>`).join("")}</div></div>`
    : "";
  const tlMatch = (e) => chars.every((id) => (e.character_ids || []).includes(id)) &&
    (!locs.length || locs.includes(e.location_id));
  const items = S.b.events.filter((r) => matches("events", r))
    .filter((e) => !S.tlChapter || e.chapter_id === S.tlChapter)
    .sort((a, b) => sortKey(a) - sortKey(b));
  const filtering = chars.length || locs.length;
  const nMatch = items.filter(tlMatch).length;
  const anchor = s.anchor_mode === "date" && s.anchor_date
    ? `Anchored at <b>${esc(s.anchor_date)}</b>` : `Relative timeline from <b>${esc(s.anchor_label || "Story start")}</b>`;
  return `<div class="hint">${anchor} · change in Series tab</div>` +
    toolbar("events", "Search events…") +
    `<div class="toolbar"><select data-act="tl-chapter">${chOpts}</select></div>` +
    pills("Characters", "characters", [...S.b.characters].sort(byName), chars) +
    pills("Places", "locations", [...S.b.locations].sort(byName), locs) +
    (filtering ? `<div class="hint tl-count">${nMatch} of ${items.length} match · <a href="#" data-act="tl-clear">Clear</a></div>` : "") +
    (items.length ? `<ul class="list tl">${items.map((e) => {
      const ages = (e.character_ids || []).map((id) => rec("characters", id)).filter(Boolean)
        .map((c) => { const a = ageAt(c, e); return `${c.name}${a !== null ? " " + a : ""}`; });
      const ch = rec("chapters", e.chapter_id), loc = rec("locations", e.location_id);
      return `<li class="${filtering && !tlMatch(e) ? "dim" : ""}" data-act="open" data-kind="events" data-id="${e.id}">
        <div class="when">${esc(whenLabel(e))}</div>
        <div class="title">${esc(e.title)}</div>
        <div class="sub">${esc([ch && "Ch " + ch.number, loc && loc.name].filter(Boolean).join(" · "))}</div>
        ${ages.length ? `<div class="ages">${esc(ages.join(" · "))}</div>` : ""}
      </li>`; }).join("")}</ul>`
      : `<div class="empty">No events yet.</div>`);
}

function todayIso() { return new Date().toISOString().slice(0, 10); }
function htmlText(html) {
  // <template>.content is an inert DocumentFragment - unlike a plain <div>,
  // setting innerHTML here never fetches/decodes any <img> the body has,
  // even briefly, since it's never part of the render tree.
  const tpl = document.createElement("template"); tpl.innerHTML = html || "";
  return (tpl.content.textContent || "").replace(/\s+/g, " ").trim();
}
function textPreview(html, max = 140) {
  const t = htmlText(html);
  return t.length > max ? t.slice(0, max) + "…" : t;
}

function listResearch() {
  const sortOpts = [["date_desc", "Newest first"], ["date_asc", "Oldest first"], ["title", "Title A–Z"]]
    .map(([v, label]) => `<option value="${v}" ${S.rsSort === v ? "selected" : ""}>${label}</option>`).join("");
  const items = S.b.research.filter((r) => matches("research", r)).sort((a, b) => {
    if (S.rsSort === "title") return (a.title || "").localeCompare(b.title || "");
    const cmp = (a.date_entered || "").localeCompare(b.date_entered || "");
    return S.rsSort === "date_asc" ? cmp : -cmp;
  });
  return toolbar("research", "Search research…", `<select data-act="research-sort">${sortOpts}</select>`) +
    (items.length ? `<ul class="list">${items.map((r) => {
      const links = [rec("chapters", r.chapter_id) && "Ch " + rec("chapters", r.chapter_id).number,
        ...(r.character_ids || []).map(charName),
        ...(r.location_ids || []).map((id) => rec("locations", id)?.name),
        ...(r.event_ids || []).map((id) => rec("events", id)?.title)].filter(Boolean);
      return `<li data-act="open" data-kind="research" data-id="${r.id}">
        <div class="title">${esc(r.title)} ${r.date_entered ? `<span class="sub">· ${esc(r.date_entered)}</span>` : ""}</div>
        <div class="sub">${esc(textPreview(r.body))}</div>
        ${links.length ? `<div class="sub">${esc(links.join(" · "))}</div>` : ""}
      </li>`; }).join("")}</ul>`
      : `<div class="empty">No research entries yet.</div>`);
}

// relationships
function relsFor(cid) {
  return S.b.relationships.filter((r) => r.from === cid || r.to === cid);
}
function relSentence(r) { return `${charName(r.from)} ${r.type} ${charName(r.to)}`; }

// ---------------------------------------------------------------- forms
function field(key, label, val, type = "text", extra = "", required = false) {
  const labelHtml = `${esc(label)}${required ? '<span class="req">*</span>' : ""}`;
  if (type === "textarea")
    return `<label><span>${labelHtml}</span><textarea data-f="${key}" ${extra}>${esc(val)}</textarea></label>`;
  return `<label><span>${labelHtml}</span><input type="${type}" data-f="${key}" value="${esc(val)}" ${extra}></label>`;
}
function chipPicker(key, all, selected) {
  return `<div class="chips" data-chips="${key}">${all.map((x) =>
    `<span class="chip ${selected.includes(x.id) ? "on" : ""}" data-act="chip" data-id="${x.id}">${esc(x.name || x.title)}</span>`).join("")
    || `<span class="hint">None yet</span>`}</div>`;
}
function formBar(kind, isNew) {
  return `<div class="formbar"><div>${isNew ? "" :
    `<button class="danger" data-act="delete" data-kind="${kind}">Delete</button>`}</div>
    <div><button data-act="cancel">Cancel</button> <button class="primary" data-act="save" data-kind="${kind}">Save</button></div></div>`;
}

function renderForm() {
  const { kind, id } = S.view;
  const r = id ? rec(kind, id) : {};
  const back = `<button class="link back" data-act="cancel">← Back</button>`;
  if (kind === "characters") return back + characterForm(r, !id);
  if (kind === "locations") return back + locationForm(r, !id);
  if (kind === "events") return back + eventForm(r, !id);
  if (kind === "research") return back + researchForm(r, !id);
  if (kind === "feedback") return back + feedbackForm(S.view.feedbackKind);
  return "";
}

// #128: the server stores age as free text (older data and imports may hold "30s" or
// "unknown"), but the Age field is a number input, which shows - and so would save -
// such a value as blank. A non-numeric stored age is kept until a number is entered.
const legacyAge = (c) => (c && c.age && !/^\d+$/.test(String(c.age).trim()) ? String(c.age) : "");

function characterForm(c, isNew) {
  const tmpl = S.b.series.character_fields || [];
  const custom = c.custom || {};
  const keys = [...tmpl, ...Object.keys(custom).filter((k) => !tmpl.includes(k))];
  const others = S.b.characters.filter((x) => x.id !== c.id).sort(byName);
  const evs = S.b.events.filter((e) => (e.character_ids || []).includes(c.id))
    .sort((a, b) => sortKey(a) - sortKey(b));
  return `<form data-kind="characters">
    <h2>${isNew ? "New character" : esc(c.name)}</h2>
    ${host.hasDocument && !isNew ? `<div class="toolbar"><button type="button" class="small" data-act="insert" data-text="${esc(c.name)}">Insert name at cursor</button></div>` : ""}
    <div class="grid2">${field("name", "Name", c.name, "text", "", true)}${field("role", "Role", c.role, "text", 'placeholder="e.g. Protagonist"')}</div>
    ${field("aliases", "Nicknames / aliases (comma separated)", c.aliases)}
    <h3>Basics</h3>
    <div class="grid3">${field("age", "Age at start", legacyAge(c) ? "" : c.age, "number")}${field("height", "Height", c.height)}${field("gender", "Gender", c.gender)}</div>
    <div class="grid3">${field("hair", "Hair", c.hair)}${field("eyes", "Eyes", c.eyes)}${field("style", "Style", c.style, "text", 'placeholder="goth, natural…"')}</div>
    ${legacyAge(c) ? `<div class="hint" data-role="legacy-age">The age stored for this character, "${esc(legacyAge(c))}", isn't a number. It is kept until you enter one.</div>` : ""}
    <h3>Physical detail</h3>
    <div class="kv" id="customFields">${keys.map((k) => `
      <span class="hint" style="margin:6px 0 0">${esc(k)}</span>
      <input data-custom="${esc(k)}" value="${esc(custom[k] || "")}">
      <span></span>`).join("")}</div>
    <div class="addrow" style="grid-template-columns:1fr auto"><input id="newFieldName" placeholder="Add a one-off field…"><button type="button" data-act="add-field">Add</button></div>
    <div class="hint">Default fields for every character are set in the Series tab.</div>
    <h3>Preferences</h3>
    ${field("preferences", "Likes, turn-ons, limits", c.preferences, "textarea")}
    <h3>Backstory</h3>
    ${field("backstory", "Backstory", c.backstory, "textarea", 'rows="5"')}
    ${field("notes", "Notes", c.notes, "textarea")}
    ${isNew ? `<div class="hint">Save first, then add relationships.</div>` : `
    <h3>Relationships</h3>
    ${relsFor(c.id).map((r) => `<div class="rel"><div>${esc(relSentence(r))}${r.note ? `<div class="note">${esc(r.note)}</div>` : ""}</div>
      <button type="button" class="small danger" data-act="del-rel" data-id="${r.id}">×</button></div>`).join("") || `<div class="hint">None yet.</div>`}
    <div class="addrow">
      <input list="relTypes" id="relType" placeholder="married to…">
      <select id="relTo"><option value="">Who?</option>${others.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
      <button type="button" data-act="add-rel">Add</button>
    </div>
    <input id="relNote" placeholder="Note (optional)" style="margin-top:4px">
    <datalist id="relTypes">${[...new Set([...REL_TYPES, ...(S.b.series.relationship_types || [])])]
      .map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
    <div class="hint">Reads as: <b>${esc(c.name)}</b> [type] [who]</div>
    <h3>Appears in timeline</h3>
    ${evs.map((e) => { const a = ageAt(c, e); return `<div class="rel"><div><span class="when">${esc(whenLabel(e))}</span> ${esc(e.title)}${a !== null ? ` <span class="note">(age ${a})</span>` : ""}</div></div>`; }).join("") || `<div class="hint">Not in any events yet.</div>`}`}
    ${formBar("characters", isNew)}
  </form>`;
}

function locationForm(l, isNew) {
  return `<form data-kind="locations">
    <h2>${isNew ? "New place" : esc(l.name)}</h2>
    ${field("name", "Name", l.name, "text", 'placeholder="e.g. The lake house"', true)}
    ${field("place", "Where", l.place, "text", 'placeholder="e.g. Terrigal, NSW"')}
    ${field("description", "Description", l.description, "textarea", 'rows="4"')}
    ${field("relevance", "Relevance to the story", l.relevance, "textarea")}
    <h3>Characters connected here</h3>
    ${chipPicker("character_ids", [...S.b.characters].sort(byName), l.character_ids || [])}
    ${field("notes", "Notes", l.notes, "textarea")}
    ${formBar("locations", isNew)}
  </form>`;
}

function eventForm(e, isNew) {
  const s = S.b.series;
  const chOpts = `<option value="">—</option>` + [...S.b.chapters].sort((a, b) => (a.number || 0) - (b.number || 0))
    .map((c) => `<option value="${c.id}" ${e.chapter_id === c.id ? "selected" : ""}>Ch ${esc(c.number)} – ${esc(c.title)}</option>`).join("");
  const lOpts = `<option value="">—</option>` + [...S.b.locations].sort(byName)
    .map((l) => `<option value="${l.id}" ${e.location_id === l.id ? "selected" : ""}>${esc(l.name)}</option>`).join("");
  return `<form data-kind="events">
    <h2>${isNew ? "New event" : esc(e.title)}</h2>
    ${field("title", "What happens", e.title, "text", "", true)}
    <h3>When (after ${esc(s.anchor_mode === "date" && s.anchor_date ? s.anchor_date : (s.anchor_label || "story start"))})</h3>
    <div class="grid3">${field("off_y", "Years", e.off_y ?? 0, "number")}${field("off_m", "Months", e.off_m ?? 0, "number")}${field("off_d", "Days", e.off_d ?? 0, "number")}</div>
    <div class="hint" id="whenPreview"></div>
    <div class="hint">Use negative numbers for backstory before the start.</div>
    <div class="grid2">
      <label><span>Chapter</span><select data-f="chapter_id">${chOpts}</select></label>
      <label><span>Place</span><select data-f="location_id">${lOpts}</select></label>
    </div>
    <h3>Who's involved</h3>
    ${chipPicker("character_ids", [...S.b.characters].sort(byName), e.character_ids || [])}
    ${field("description", "Details", e.description, "textarea", 'rows="4"')}
    ${formBar("events", isNew)}
  </form>`;
}

function researchForm(r, isNew) {
  const chOpts = `<option value="">(no chapter)</option>` + [...S.b.chapters].sort((a, b) => (a.number || 0) - (b.number || 0))
    .map((c) => `<option value="${c.id}" ${r.chapter_id === c.id ? "selected" : ""}>Ch ${esc(c.number)} – ${esc(c.title)}</option>`).join("");
  return `<form data-kind="research">
    <h2>${isNew ? "New research entry" : esc(r.title)}</h2>
    <div class="grid2">${field("title", "Title", r.title, "text", "", true)}
      ${field("date_entered", "Date entered", isNew ? todayIso() : r.date_entered, "date")}</div>
    <label><span>Chapter</span><select data-f="chapter_id">${chOpts}</select></label>
    <h3>Notes</h3>
    <div id="researchEditor"></div>
    <textarea data-f="body" hidden>${esc(r.body || "")}</textarea>
    <div class="hint">Images are resized automatically when added.</div>
    <h3>Linked characters</h3>
    ${chipPicker("character_ids", [...S.b.characters].sort(byName), r.character_ids || [])}
    <h3>Linked places</h3>
    ${chipPicker("location_ids", [...S.b.locations].sort(byName), r.location_ids || [])}
    <h3>Linked timeline events</h3>
    ${chipPicker("event_ids", [...S.b.events].sort((a, b) => sortKey(a) - sortKey(b)), r.event_ids || [])}
    ${formBar("research", isNew)}
  </form>`;
}

// ------------------------------------------------------- research WYSIWYG
// No explicit teardown of a previous Quill instance: the whole form is
// replaced via innerHTML on every render() (same as every other form in
// this file), which discards its DOM. Quill has no public destroy() API
// (see its FAQ) - the one thing that leaks is its document-level
// selectionchange listener, which is harmless for a single long-lived tab.
function initResearchEditor(r) {
  const el = $("#researchEditor"); if (!el) return;
  const textarea = $('textarea[data-f="body"]');
  const quill = new Quill(el, {
    theme: "snow",
    modules: {
      toolbar: {
        container: [["bold", "italic", "underline", "strike"], [{ header: [1, 2, 3, false] }],
          ["blockquote"], [{ list: "ordered" }, { list: "bullet" }], ["link", "image"], ["clean"]],
        handlers: { image: quillImageHandler },
      },
      // Quill's own paste/drag-drop handling (Clipboard -> Uploader, see
      // vendor/quill/quill.js) inserts images straight from FileReader with
      // no resizing or size cap - route it through the same compression as
      // the toolbar button, or a pasted screenshot skips both entirely.
      // mimetypes matches what compressImage()/the sanitizer's data-URL
      // allowlist accept (png/jpeg/gif/webp) - Quill's own default is
      // narrower (png/jpeg only) and would otherwise silently drop the rest
      // before the handler below ever runs.
      uploader: { mimetypes: ["image/png", "image/jpeg", "image/gif", "image/webp"], handler: quillUploadHandler },
    },
  });
  quill.clipboard.dangerouslyPasteHTML(0, r.body || "");
  quill.on("text-change", () => { textarea.value = quill.root.innerHTML; });
}

const MAX_IMAGE_DIM = 1600;      // longest edge, px
const MAX_IMAGE_QUALITY = 0.82;  // JPEG quality
const MAX_IMAGE_DATA_URL_CHARS = 3_500_000;  // ~2.6MB decoded - keeps a few images well under MAX_BODY_BYTES

function compressImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Couldn't read the file"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("Not a valid image"));
      img.onload = () => {
        let { width, height } = img;
        if (width > MAX_IMAGE_DIM || height > MAX_IMAGE_DIM) {
          const scale = MAX_IMAGE_DIM / Math.max(width, height);
          width = Math.round(width * scale); height = Math.round(height * scale);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width; canvas.height = height;
        canvas.getContext("2d").drawImage(img, 0, 0, width, height);
        const isPng = file.type === "image/png";
        resolve(canvas.toDataURL(isPng ? "image/png" : "image/jpeg", isPng ? undefined : MAX_IMAGE_QUALITY));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

async function insertCompressedImages(quill, index, files) {
  for (const file of files) {
    try {
      const dataUrl = await compressImage(file);
      if (dataUrl.length > MAX_IMAGE_DATA_URL_CHARS) { toast("An image was skipped - still too large after resizing"); continue; }
      quill.insertEmbed(index, "image", dataUrl, "user");
      index += 1;
    } catch (err) { toast(err.message); }
  }
  quill.setSelection(index);
}

function quillImageHandler() {
  const input = document.createElement("input");
  input.type = "file"; input.accept = "image/*";
  input.onchange = () => {
    const file = input.files[0]; if (!file) return;
    const range = this.quill.getSelection(true) || { index: this.quill.getLength() };
    insertCompressedImages(this.quill, range.index, [file]);
  };
  input.click();
}

// modules.uploader handler - called by Quill itself on paste/drag-drop of
// image files (see the comment where this is registered above).
function quillUploadHandler(range, files) {
  insertCompressedImages(this.quill, range.index, files);
}

// #148: sharing - add by exact email only (never a lookup/browse of other
// users - the email picker goes through GET /users/lookup, #88's rate-
// limited, no-enumeration primitive, same flow tests/test_ownership.py's
// test_lookup_then_share_with_a_stranger already exercises server-side).
// Choose edit or view, revoke any time from either side. Outside the
// settings <form>, like Chapters/Backup/Feedback below - each action here
// is immediate, not batched into one Save.
function sharingSection() {
  if (S.config?.authMode !== "entra" || !S.members) return "";
  const myOid = S.me?.oid || "";
  const isOwner = S.b.series.owner_oid === myOid;
  if (isOwner) {
    const rows = S.members.members.map((m) => `<div class="addrow" style="grid-template-columns:1fr auto auto;align-items:center">
      <div>${esc(m.display_name || m.oid)}${m.email ? `<div class="note">${esc(m.email)}</div>` : ""}</div>
      <select data-act="share-role" data-oid="${esc(m.oid)}">
        <option value="viewer" ${m.role === "viewer" ? "selected" : ""}>Can view</option>
        <option value="editor" ${m.role === "editor" ? "selected" : ""}>Can edit</option>
      </select>
      <button type="button" class="small danger" data-act="share-remove" data-oid="${esc(m.oid)}">Remove</button>
    </div>`).join("") || `<div class="hint">Not shared with anyone yet.</div>`;
    return `<h3>Sharing</h3>
      ${rows}
      <div class="addrow" style="grid-template-columns:1fr auto auto">
        <input id="shareEmail" type="email" placeholder="Their exact sign-in email">
        <select id="shareRole"><option value="viewer">Can view</option><option value="editor">Can edit</option></select>
        <button type="button" data-act="share-add">Share</button>
      </div>
      <div class="hint">You must know the exact email they sign in with - there's no list of other users to browse.</div>`;
  }
  const mine = S.members.members.find((m) => m.oid === myOid);
  const ownerLabel = S.members.owner_email
    ? `${esc(S.members.owner_display_name)} (${esc(S.members.owner_email)})`
    : esc(S.members.owner_display_name);
  return `<h3>Sharing</h3>
    <div class="hint">Shared by ${ownerLabel} - you can ${mine?.role === "editor" ? "edit" : "view"}.</div>
    <button type="button" class="small danger" data-act="share-leave">Leave this series</button>`;
}

function seriesForm() {
  const s = S.b.series;
  const chapters = [...S.b.chapters].sort((a, b) => (a.number || 0) - (b.number || 0));
  return `<form data-kind="series">
    <h2>Series settings</h2>
    ${field("name", "Series name", s.name)}
    ${field("description", "Premise / notes", s.description, "textarea")}
    <h3>Timeline anchor</h3>
    <label><span>Anchor type</span><select data-f="anchor_mode">
      <option value="relative" ${s.anchor_mode !== "date" ? "selected" : ""}>Relative (no real dates)</option>
      <option value="date" ${s.anchor_mode === "date" ? "selected" : ""}>Real calendar date</option></select></label>
    <div class="grid2">${field("anchor_date", "Start date", s.anchor_date, "date")}${field("anchor_label", "Start label", s.anchor_label, "text", 'placeholder="Story start"')}</div>
    <div class="hint">Events are stored as offsets, so changing the anchor moves the whole timeline.</div>
    <h3>Default character fields</h3>
    <textarea data-f="character_fields" rows="6">${esc((s.character_fields || []).join("\n"))}</textarea>
    <div class="hint">One per line. These appear on every character in this series.</div>
    <h3>Relationship type suggestions</h3>
    <textarea data-f="relationship_types" rows="6">${esc((s.relationship_types || []).join("\n"))}</textarea>
    <div class="hint">One per line, added to the built-in suggestions (married to, friend of, etc.) while typing a relationship's type - you can always type something else too.</div>
    <div class="formbar"><div><button type="button" class="danger" data-act="delete-series">Delete series</button></div>
      <div><button class="primary" data-act="save" data-kind="series">Save</button></div></div>
  </form>
  ${sharingSection()}
  <h3>Chapters (one Word doc each)</h3>
  ${chapters.map((c) => `<div class="rel"><div>Ch ${esc(c.number)} – ${esc(c.title)}</div>
    <button type="button" class="small danger" data-act="del-chapter" data-id="${c.id}">×</button></div>`).join("") || `<div class="hint">None yet.</div>`}
  <div class="addrow" style="grid-template-columns:60px 1fr auto">
    <input id="chNum" type="number" placeholder="#" value="${chapters.length + 1}"><input id="chTitle" placeholder="Chapter title"><button type="button" data-act="add-chapter">Add</button></div>
  <h3>Backup</h3>
  <div class="toolbar"><button type="button" data-act="export">Export JSON</button>
    <label style="margin:0" class="small"><button type="button" data-act="import-pick">Import JSON…</button>
    <input type="file" id="importFile" accept=".json" hidden></label></div>
  <h3>Feedback</h3>
  <div class="toolbar"><button type="button" data-act="log-issue">Log Issue</button>
    <button type="button" data-act="log-suggestion">Log Suggestion</button></div>
  ${buildInfoHtml()}
  ${S.config?.authMode === "token" ? `
  <h3>Connection</h3>
  <label><span>API token (only if the server sets STORYBIBLE_TOKEN)</span>
    <input id="tokenInput" type="password" value="${esc(lsGet("sb_token"))}"></label>
  <button type="button" data-act="save-token">Save token</button>` : ""}`;
}

function fmtBytes(n) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`; }
function fmtWhen(t) { return t ? new Date(t * 1000).toLocaleString() : "never"; }
function adminView() {
  const list = S.adminUsers;
  if (S.adminError) return `<h2>Users</h2><div class="hint">Couldn't load the user list: ${esc(S.adminError)}</div>
    <button type="button" data-act="open-admin">Try again</button>`;
  if (!list) return `<h2>Users</h2><div class="hint">Loading…</div>`;
  return `<h2>Users (${list.total})</h2>
    <div class="hint">Account details and amounts only - story content is never shown. "Last active" is approximate.${
      list.users.length < list.total ? ` Showing the ${list.users.length} most recently active.` : ""}</div>
    ${list.users.map((u) => `<div class="rel" style="align-items:flex-start">
      <div><b>${esc(u.display_name || u.oid)}</b>${u.blocked ? ` <span class="note">(blocked${u.blocked_reason ? ": " + esc(u.blocked_reason) : ""})</span>` : ""}
        <div class="note">${esc(u.email)}</div>
        <div class="note">Last active ${esc(fmtWhen(u.last_seen))} · joined ${esc(fmtWhen(u.first_seen))}</div>
        <div class="note">${u.series_owned} series · ${Object.entries(u.records).filter(([, n]) => n).map(([k, n]) => `${n} ${esc(k)}`).join(", ") || "no records"} · ${fmtBytes(u.bytes_used)}${u.series_shared ? ` · shared into ${u.series_shared}` : ""}</div></div>
      ${u.oid === S.me?.oid ? "" : u.blocked
        ? `<button type="button" class="small" data-act="admin-unblock" data-oid="${esc(u.oid)}">Unblock</button>`
        : `<button type="button" class="small danger" data-act="admin-block" data-oid="${esc(u.oid)}">Block</button>`}</div>`).join("")}`;
}
function accountView() {
  const who = S.me?.email || msalAccount?.username || "";
  return `<h2>Your account</h2>
    <div class="hint">Signed in as ${esc(msalAccount?.name || who || "you")}${who ? ` (${esc(who)})` : ""}.</div>
    <h3>Download my data</h3>
    <div class="hint">A JSON file with your account details and every series you own.</div>
    <button type="button" data-act="download-my-data">Download my data</button>
    <h3>Delete my account</h3>
    <div class="hint">This permanently deletes your account and all the series you own, with their characters, places, timeline, research and chapters. Anyone you shared a series with loses access to it. You will also be removed from series other people shared with you, and your name on their entries becomes "Deleted user".</div>
    <div class="hint">Nightly backups keep a copy for a few days before it ages out. Feedback you filed is public on GitHub and can't be removed from here.</div>
    <label><span>${S.me?.email ? "Type your email address to confirm" : "Type DELETE to confirm"}</span>
      <input id="deleteConfirm" type="text" autocomplete="off" value=""></label>
    <button type="button" class="danger" data-act="delete-account">Delete my account and data</button>${legalLinksHtml()}`;
}
async function loadAdminUsers() {
  S.adminError = null;
  try { S.adminUsers = await api("/admin/users"); }
  catch (err) { S.adminError = err.message; }
  if (S.view?.kind === "admin") render();
}

// The build stamp (#144), e.g. "Build v0.1.0-12-gabc1234 · 2026-10-03"; "" if the
// server didn't report one.
function buildInfoText() {
  const v = S.config?.buildVersion;
  return v ? `Build ${v}${S.config.buildDate ? ` · ${S.config.buildDate}` : ""}` : "";
}
function buildInfoHtml() {
  const t = buildInfoText();
  return t ? `<div class="hint" data-role="build-info">${esc(t)}</div>` : "";
}
function feedbackForm(kind) {
  const label = kind === "issue" ? "Log an issue" : "Log a suggestion";
  return `<form data-kind="feedback" data-feedback-kind="${kind}">
    <h2>${label}</h2>
    ${field("title", "Title", "", "text", 'maxlength="120" placeholder="Short summary"', true)}
    <div class="hint">3–120 characters.</div>
    ${field("description", "Description", "", "textarea", 'rows="6" maxlength="4000" placeholder="What happened, or what you\'d like to see"', true)}
    <div class="hint" data-role="public-notice"><b>This is posted as a public GitHub issue</b>, so anyone can read it.
      Don't include story text, character or place names from your series, or anything private.${
        S.config?.authMode === "entra" ? " Your display name is added to it." : ""}${
        buildInfoText() ? " The build version and date are added too." : ""}</div>
    ${buildInfoHtml()}
    <div class="formbar"><div></div>
      <div><button type="button" data-act="cancel">Cancel</button>
      <button class="primary" data-act="save-feedback">${label}</button></div></div>
  </form>`;
}

// ----------------------------------------------------------- form reading
function readForm(form) {
  const data = {};
  form.querySelectorAll("[data-f]").forEach((el) => { data[el.dataset.f] = el.value; });
  form.querySelectorAll("[data-chips]").forEach((el) => {
    data[el.dataset.chips] = [...el.querySelectorAll(".chip.on")].map((c) => c.dataset.id);
  });
  const custom = {};
  form.querySelectorAll("[data-custom]").forEach((el) => { if (el.value.trim()) custom[el.dataset.custom] = el.value; });
  if (form.dataset.kind === "characters") data.custom = custom;
  if (form.dataset.kind === "series") {
    data.character_fields = data.character_fields.split("\n").map((x) => x.trim()).filter(Boolean);
    data.relationship_types = data.relationship_types.split("\n").map((x) => x.trim()).filter(Boolean);
  }
  if (form.dataset.kind === "events") ["off_y", "off_m", "off_d"].forEach((k) => { data[k] = Number(data[k]) || 0; });
  return data;
}

function updateWhenPreview() {
  const f = $("form[data-kind=events]"); if (!f) return;
  const d = readForm(f); $("#whenPreview").textContent = "→ " + whenLabel(d);
}

// ------------------------------------------------------------------ actions
async function onClick(ev) {
  const t = ev.target.closest("[data-act]"); if (!t) return;
  const act = t.dataset.act;
  if (["save", "chip", "tl-chip", "tl-clear", "add-field", "add-rel", "del-rel", "delete", "delete-series",
       "add-chapter", "del-chapter", "cancel", "insert", "export", "import-pick", "save-token",
       "log-issue", "log-suggestion", "save-feedback", "sign-in", "sign-out",
       "open-admin", "admin-block", "admin-unblock",
       "share-add", "share-remove", "share-leave"].includes(act)) ev.preventDefault();
  try {
    switch (act) {
      case "open": S.view = { kind: t.dataset.kind, id: t.dataset.id }; render(); window.scrollTo(0, 0); break;
      case "new": S.view = { kind: t.dataset.kind, id: null }; render(); break;
      case "cancel": if (!(await confirmDiscard())) return; S.view = null; render(); revalidateBundle(); break;
      case "chip": t.classList.toggle("on"); break;
      case "tl-chip": {
        const key = t.dataset.kind === "characters" ? "tlChars" : "tlLocs", id = t.dataset.id;
        S[key] = S[key].includes(id) ? S[key].filter((x) => x !== id) : [...S[key], id];
        render(); break;
      }
      case "tl-clear": S.tlChars = []; S.tlLocs = []; render(); break;
      case "modal-cancel": closeModal(false); break;
      case "modal-confirm": closeModal(true); break;
      case "new-series": await newSeries(); break;
      case "save": await save(t.dataset.kind, t.closest("form")); break;
      case "delete": {
        if (!t.classList.contains("armed")) { t.classList.add("armed"); t.textContent = "Confirm delete"; return; }
        await api(`/series/${S.sid}/${t.dataset.kind}/${S.view.id}`, "DELETE");
        S.view = null; await loadBundle(); render(); toast("Deleted"); break;  // full reload: the server cascades
      }
      case "delete-series": {
        if (!t.classList.contains("armed")) { t.classList.add("armed"); t.textContent = "Delete EVERYTHING in this series?"; return; }
        await api(`/series/${S.sid}`, "DELETE");
        S.seriesList = S.seriesList.filter((s) => s.id !== S.sid);
        await selectSeries(S.seriesList[0]?.id); toast("Series deleted"); break;
      }
      case "add-field": {
        const name = $("#newFieldName").value.trim(); if (!name) return;
        $("#customFields").insertAdjacentHTML("beforeend", customFieldHtml(name));
        $("#newFieldName").value = ""; break;
      }
      case "add-rel": {
        const type = $("#relType").value.trim(), to = $("#relTo").value;
        if (!type || !to) { toast("Pick a type and a character"); return; }
        const created = await api(`/series/${S.sid}/relationships`, "POST",
          { data: { from: S.view.id, to, type, note: $("#relNote").value.trim() } });
        putLocal("relationships", created); rerenderKeepingEdits(); break;
      }
      case "del-rel": {
        await api(`/series/${S.sid}/relationships/${t.dataset.id}`, "DELETE");
        // Nothing references a relationship, so there's no server cascade to fetch.
        S.b.relationships = S.b.relationships.filter((x) => x.id !== t.dataset.id); S.bEtag = null; bundleGen++;
        rerenderKeepingEdits(); break;
      }
      case "add-chapter": {
        const title = $("#chTitle").value.trim(); if (!title) return;
        putLocal("chapters", await api(`/series/${S.sid}/chapters`, "POST", { data: { number: Number($("#chNum").value) || 0, title } }));
        rerenderKeepingEdits(); break;
      }
      case "del-chapter": await api(`/series/${S.sid}/chapters/${t.dataset.id}`, "DELETE"); await loadBundle(); rerenderKeepingEdits(); break;
      case "share-add": {
        const email = $("#shareEmail").value.trim(), role = $("#shareRole").value;
        if (!email) { toast("Enter their exact sign-in email"); return; }
        // Same two-step flow test_lookup_then_share_with_a_stranger exercises
        // server-side: resolve the one exact email via the rate-limited
        // lookup, then PUT the member - never a browsable list.
        const found = await api(`/users/lookup?email=${encodeURIComponent(email)}`);
        await api(`/series/${S.sid}/members/${found.oid}`, "PUT", { role });
        await loadMembers(); rerenderKeepingEdits(); toast(`Shared with ${found.display_name}`);
        break;
      }
      case "share-remove":
        await api(`/series/${S.sid}/members/${t.dataset.oid}`, "DELETE");
        await loadMembers(); rerenderKeepingEdits(); toast("Removed"); break;
      case "share-leave": {
        // Leaving removes the current user's own access, so there's
        // nothing left to re-render for this series - go back to the
        // series list the way deleting a series already does.
        await api(`/series/${S.sid}/members/${S.me?.oid}`, "DELETE");
        await loadSeriesList(); await selectSeries(S.seriesList[0]?.id);
        toast("You left this series"); break;
      }
      case "doc-link": saveDocLink({ series_id: S.sid, chapter_id: "" }); render(); break;
      case "insert": await host.insertText(t.dataset.text); break;
      case "export": {
        const blob = new Blob([JSON.stringify(S.b, null, 2)], { type: "application/json" });
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
        a.download = `${S.b.series.name.replace(/[^\w-]+/g, "_")}.storybible.json`; a.click(); break;
      }
      case "import-pick": $("#importFile").click(); break;
      case "save-token": lsSet("sb_token", $("#tokenInput").value.trim()); await boot(); toast("Token saved"); break;
      case "sign-in": await signIn(); if (msalAccount) { await loadApp(); } else { renderHeader(); } break;
      case "accept-policy":
        S.me = await api("/me/accept-policy", "POST", { version: S.config.policyVersion });
        await loadApp();
        break;
      case "sign-out": await signOut(); S.b = null; S.me = null; S.view = null; S.seriesList = []; render(); break;
      case "open-account": S.view = { kind: "account", id: null }; render(); break;
      case "download-my-data": {
        const blob = new Blob([JSON.stringify(await api("/me/export"), null, 2)], { type: "application/json" });
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
        a.download = "storybible-my-data.json"; a.click(); break;
      }
      case "delete-account": {
        const typed = $("#deleteConfirm").value.trim();
        if (!typed || typed.toLowerCase() !== (S.me?.email || "DELETE").trim().toLowerCase()) { toast(S.me?.email ? "Type your email address to confirm" : "Type DELETE to confirm", "error"); return; }
        await api("/me", "DELETE", { confirm: typed });
        await signOut(); S.b = null; S.me = null; S.view = null; S.seriesList = []; render();
        toast("Your account and data have been deleted"); break;
      }
      case "open-admin": S.view = { kind: "admin", id: null }; S.adminUsers = null; S.adminError = null; render(); await loadAdminUsers(); break;
      case "admin-block": {
        const u = S.adminUsers.users.find((x) => x.oid === t.dataset.oid);
        if (!(await showModal(`Block ${u?.display_name || "this user"}? They will be refused until you unblock them. Their data is kept.`, "Block"))) return;
        await api(`/admin/users/${encodeURIComponent(t.dataset.oid)}/block`, "PUT", { reason: "" });
        await loadAdminUsers(); toast("Blocked"); break;
      }
      case "admin-unblock":
        await api(`/admin/users/${encodeURIComponent(t.dataset.oid)}/block`, "DELETE");
        await loadAdminUsers(); toast("Unblocked"); break;
      case "log-issue": S.view = { kind: "feedback", id: null, feedbackKind: "issue" }; render(); break;
      case "log-suggestion": S.view = { kind: "feedback", id: null, feedbackKind: "suggestion" }; render(); break;
      case "save-feedback": {
        const form = t.closest("form");
        const data = readForm(form);
        const title = (data.title || "").trim(), description = (data.description || "").trim();
        if (title.length < 3 || title.length > 120) { toast("Title must be 3–120 characters"); return; }
        if (!description) { toast("Description is required"); return; }
        const kind = form.dataset.feedbackKind;
        t.disabled = true;
        const original = t.textContent;
        t.textContent = "Filing…";
        try {
          const r = await api("/feedback", "POST", { kind, title, description });
          toast(`Filed as #${r.number}`);
          S.view = null; render();
        } finally {
          t.disabled = false; t.textContent = original;
        }
        break;
      }
    }
  } catch (err) { toast(err.message, "error"); console.error(err); }
}

async function newSeries() {
  const s = await api("/series", "POST", { data: { name: "New series", anchor_mode: "relative",
    anchor_label: "Story start", character_fields: DEFAULT_FIELDS } });
  await loadSeriesList(); S.tab = "series"; await selectSeries(s.id); toast("Series created — name it here");
}

// #58: on a 409, offer the real choice rather than just a toast - Reload
// (the response already carries the current record, so this is free: no
// second round trip) or Keep mine, which does nothing further and leaves
// the form exactly as the user left it. There's no third "force overwrite"
// option: saving again while still holding the stale version just 409s
// again, which is deliberate - "never overwrite silently" (#14/#58).
async function handleSaveConflict(err, applyCurrent) {
  if (await showModal(err.message, "Reload")) { await applyCurrent(err.detail.current); render(); }
}

async function save(kind, form) {
  const data = readForm(form);
  if (kind === "series") {
    // #12: PUT must send back the version this was loaded at, so a save
    // from a stale copy (someone else changed it meanwhile) 409s instead
    // of silently overwriting their edit.
    let saved;
    try {
      saved = await api(`/series/${S.sid}`, "PUT", { data: { ...S.b.series, ...data }, version: S.b.series.version });
    } catch (err) {
      if (err.status !== 409) throw err;
      await handleSaveConflict(err, async (current) => { S.b.series = current; await loadSeriesList(); });
      return;
    }
    // The PUT returns the saved series: patch the bundle and the picker from it (#72).
    S.b.series = saved; S.bEtag = null; bundleGen++;
    const inList = S.seriesList.find((s) => s.id === saved.id);
    if (inList) {
      Object.assign(inList, { name: saved.name, version: saved.version, updated: saved.updated });
      S.seriesList.sort((a, b) => (a.name || "").toLowerCase().localeCompare((b.name || "").toLowerCase()));
    }
    render(); toast("Saved"); return;
  }
  if ((kind === "characters" || kind === "locations") && !data.name?.trim()) { toast("Name is required"); return; }
  if ((kind === "events" || kind === "research") && !data.title?.trim()) {
    toast(kind === "events" ? "Say what happens" : "Title is required"); return;
  }
  if (S.view.id) {
    const prev = rec(kind, S.view.id);
    if (kind === "characters" && !data.age && legacyAge(prev)) data.age = prev.age;  // #128
    let saved;
    try {
      saved = await api(`/series/${S.sid}/${kind}/${S.view.id}`, "PUT", { data: { ...prev, ...data }, version: prev.version });
    } catch (err) {
      if (err.status !== 409) throw err;
      await handleSaveConflict(err, (current) => {
        const idx = S.b[kind].findIndex((r) => r.id === S.view.id);
        if (idx >= 0) S.b[kind][idx] = current;
      });
      return;
    }
    putLocal(kind, saved); render();
  } else {
    const r = await api(`/series/${S.sid}/${kind}`, "POST", { data });
    putLocal(kind, r); S.view = kind === "characters" ? { kind, id: r.id } : null; render();
  }
  toast("Saved");
}

async function findSelection() {
  const txt = (await host.getSelection()).toLowerCase();
  if (!txt) { toast("Select a name in the document first"); return; }
  const names = (r) => [r.name, ...(r.aliases || "").split(",")].map((x) => (x || "").trim().toLowerCase()).filter(Boolean);
  const c = S.b.characters.find((r) => names(r).includes(txt));
  const l = S.b.locations.find((r) => (r.name || "").toLowerCase() === txt);
  if (c) { S.tab = "characters"; S.view = { kind: "characters", id: c.id }; }
  else if (l) { S.tab = "locations"; S.view = { kind: "locations", id: l.id }; }
  else { S.tab = "characters"; S.view = null; S.filter = txt; toast("No exact match — showing search"); }
  render();
}

// -------------------------------------------------------------------- wiring
function wire() {
  document.addEventListener("click", onClick);
  document.addEventListener("submit", (e) => e.preventDefault());
  document.addEventListener("input", (e) => {
    if (e.target.dataset.act === "filter") {
      S.filter = e.target.value; const pos = e.target.selectionStart; render();
      const f = $("[data-act=filter]"); f.focus(); f.setSelectionRange(pos, pos);
    }
    if (e.target.closest("form[data-kind=events]")) updateWhenPreview();
  });
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.id === "seriesSelect") {
      const next = t.value;
      if (!(await confirmDiscard())) { t.value = S.sid || ""; return; }
      if (next === "__new") await newSeries(); else await selectSeries(next);
    }
    if (t.dataset.act === "tl-chapter") { S.tlChapter = t.value; render(); }
    if (t.dataset.act === "research-sort") { S.rsSort = t.value; render(); }
    if (t.dataset.act === "doc-chapter") saveDocLink({ series_id: S.sid, chapter_id: t.value });
    if (t.dataset.act === "share-role") {
      try {
        await api(`/series/${S.sid}/members/${t.dataset.oid}`, "PUT", { role: t.value });
        await loadMembers(); rerenderKeepingEdits(); toast("Role updated");
      } catch (err) { toast(err.message, "error"); }
    }
    if (t.id === "importFile" && t.files[0]) {
      try {
        const r = await api("/import", "POST", JSON.parse(await t.files[0].text()));
        await loadSeriesList(); await selectSeries(r.id); toast(r.dropped_references ? `Imported - ${r.dropped_references} broken link${r.dropped_references === 1 ? "" : "s"} removed` : "Imported");
      } catch (err) { toast("Import failed: " + err.message, "error"); }
    }
  });
  window.addEventListener("focus", revalidateBundle);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) revalidateBundle(); });
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", async () => {
    if (!(await confirmDiscard())) return;
    S.tab = b.dataset.tab; S.view = null; S.filter = ""; render();
    revalidateBundle();  // (a no-op when the new tab opens a form, e.g. Series)
  }));
  $("#btnHelp").addEventListener("click", () => host.openExternal(HELP_URL));
  $("#btnFind").addEventListener("click", () => findSelection().catch((e) => toast(e.message, "error")));
}

async function loadApp() {
  try {
    await loadMe();
    if (policyGateNeeded()) { render(); return; }
    await loadSeriesList();
    await readDocLink();
    const want = (S.docLink && S.docLink.series_id) || lsGet("sb_series");
    const pick = S.seriesList.find((s) => s.id === want) || S.seriesList[0];
    await selectSeries(pick?.id);
    // when the doc knows its chapter, pre-filter the timeline to it
    if (S.docLink?.chapter_id && S.docLink.series_id === S.sid) S.tlChapter = S.docLink.chapter_id;
  } catch (err) {
    $("#main").innerHTML = `<div class="empty">Can't reach the Story Bible server.<br>${esc(err.message)}</div>`;
  }
}

// Privacy / terms links under Sign in, so they are visible before anyone's first
// sign-in, and at the foot of the Account view once signed in (#112). Only
// https URLs are ever rendered (the server filters too).
function legalLinksHtml() {
  const links = [["Privacy policy", S.config.privacyUrl], ["Terms of use", S.config.termsUrl]]
    .filter(([, url]) => /^https:\/\//i.test(url || ""))
    .map(([label, url]) => `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  return links.length ? `<p class="legal-links">${links.join(" · ")}</p>` : "";
}

async function boot() {
  await initAuth();
  if (S.config.authMode === "entra" && !msalAccount) {
    // No cached account - MSAL popups need a user gesture anyway (a popup
    // opened outside a click handler is just blocked), so this only ever
    // shows a Sign in button, never auto-prompts.
    renderHeader();
    $("#main").innerHTML = `<div class="empty">Sign in to continue.<br><br>
      <button class="primary" data-act="sign-in">Sign in</button>${legalLinksHtml()}</div>`;
    return;
  }
  await loadApp();
}

async function start() {
  // If host detection throws, carry on as a plain web page rather than a blank pane.
  try { await host.init(); } catch (err) { console.error("host init failed", err); }
  document.body.classList.toggle("has-doc", host.hasDocument);
  wire(); boot();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
else start();
