/* Story Bible task pane - demo build.
 * Plain JS, no build step. Works inside Word (Office.js) and in a normal browser.
 */
"use strict";

const S = {
  inWord: false,
  seriesList: [],
  sid: null,          // current series id
  b: null,            // bundle for current series
  tab: "characters",
  view: null,         // {kind, id} when editing a record, else null (list)
  filter: "",
  tlChapter: "", tlChar: "",
  rsSort: "date_desc",  // research list: date_desc | date_asc | title
  docLink: null,      // {series_id, chapter_id} stored in the Word document
  config: null,       // {authMode, tenantId, clientId} from GET /api/config (#13)
};

// MSAL state (#13) - not on S since it holds live library objects, not
// plain data; msalAccount is the only piece the UI needs to read.
let msalPca = null;
let msalAccount = null;

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

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 1800);
}

async function api(path, method = "GET", body) {
  const headers = { "Content-Type": "application/json" };
  if (S.config?.authMode === "entra") {
    const token = await getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } else {
    const tok = lsGet("sb_token"); if (tok) headers["X-Token"] = tok;
  }
  const res = await fetch("/api" + path, { method, headers,
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    if (res.status === 401) {
      throw new Error(S.config?.authMode === "entra" ? "Signed out - sign in again" : "Needs API token (Series tab → Connection)");
    }
    const txt = await res.text();
    // #12: 409/428 (and 400s from Pydantic) carry a structured `detail`
    // rather than a plain string - surface something readable instead of
    // the raw JSON blob. Full conflict-resolution UI (reload/keep-mine) is
    // #14, not this - this is just "don't show garbage in the toast".
    let message = txt;
    try {
      const detail = JSON.parse(txt).detail;
      if (typeof detail === "string") message = detail;
      else if (res.status === 409 && detail?.error === "conflict") {
        message = `Changed by ${detail.updated_by || "someone else"} since you loaded it - reload and try again`;
      }
    } catch { /* not JSON - fall back to the raw text above */ }
    throw new Error(message);
  }
  return res.json();
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

function naaSupported() {
  return S.inWord && !!window.Office?.context?.requirements?.isSetSupported?.("NestedAppAuth", "1.1");
}

async function initAuth() {
  try {
    S.config = await (await fetch("/api/config")).json();
  } catch {
    S.config = { authMode: "none", tenantId: "", clientId: "" };  // server unreachable - boot()'s own error state takes it from here
  }
  if (S.config.authMode !== "entra") return;
  const msalConfig = {
    auth: { clientId: S.config.clientId, authority: `https://login.microsoftonline.com/${S.config.tenantId}` },
    cache: { cacheLocation: "localStorage" },  // survives the task pane closing/reopening with a document
  };
  try {
    const useNaa = !S.inWord || naaSupported();
    if (useNaa) {
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
    if (!S.inWord || naaSupported()) {
      const result = await msalPca.acquireTokenPopup({ scopes: [msalScope()] });
      msalAccount = result.account;
    } else {
      await signInViaDialog();
    }
  } catch (err) {
    toast("Sign-in failed: " + err.message);
  }
}

// Perpetual Office without NAA can't do a normal popup from the task pane,
// so the interactive part happens in a separate Office dialog (a small
// same-origin page, auth-dialog.html) running the standard redirect flow;
// this instance then just re-reads the account both pages share via the
// same localStorage cache.
function signInViaDialog() {
  return new Promise((resolve, reject) => {
    Office.context.ui.displayDialogAsync(
      window.location.origin + "/auth-dialog.html",
      { height: 60, width: 30 },
      (asyncResult) => {
        if (asyncResult.status === Office.AsyncResultStatus.Failed) {
          reject(new Error(asyncResult.error.message)); return;
        }
        const dialog = asyncResult.value;
        dialog.addEventHandler(Office.EventType.DialogMessageReceived, (arg) => {
          dialog.close();
          const msg = JSON.parse(arg.message);
          if (!msg.ok) { reject(new Error(msg.error || "Sign-in failed")); return; }
          msalAccount = msalPca.getAllAccounts()[0] || null;
          resolve();
        });
        // The user closing the dialog (or Entra sign-in erroring out before
        // messageParent ever runs) fires this instead of DialogMessageReceived -
        // without handling it too, the promise above never settles and the
        // Sign in button just looks permanently stuck on that attempt.
        dialog.addEventHandler(Office.EventType.DialogEventReceived, () => {
          reject(new Error("Sign-in cancelled"));
        });
      },
    );
  });
}

async function signOut() {
  if (msalPca && msalAccount) {
    try { await msalPca.getTokenCache().removeAccount(msalAccount); } catch { /* best-effort */ }
  }
  msalAccount = null;
}

// ---------------------------------------------------------------- Word glue
async function readDocLink() {
  if (!S.inWord) return;
  S.docLink = Office.context.document.settings.get("storybible") || null;
}
function saveDocLink(link) {
  S.docLink = link;
  Office.context.document.settings.set("storybible", link);
  Office.context.document.settings.saveAsync(() => toast("Document linked"));
}
async function wordSelection() {
  return Word.run(async (ctx) => {
    const r = ctx.document.getSelection(); r.load("text"); await ctx.sync();
    return (r.text || "").trim();
  });
}
async function insertText(text) {
  await Word.run(async (ctx) => {
    ctx.document.getSelection().insertText(text, "Replace"); await ctx.sync();
  });
}

// ------------------------------------------------------------------- loading
async function loadSeriesList() {
  S.seriesList = await api("/series");
}
async function loadBundle() {
  S.b = S.sid ? await api(`/series/${S.sid}/bundle`) : null;
}
async function selectSeries(id) {
  S.sid = id || null; S.view = null; lsSet("sb_series", S.sid || "");
  await loadBundle(); render();
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
    ? `<span>${esc(msalAccount.name || msalAccount.username || "Signed in")}</span><button class="small" data-act="sign-out">Sign out</button>`
    : `<button class="small primary" data-act="sign-in">Sign in</button>`);

  const lb = $("#linkBar");
  if (!S.inWord || !S.b) { lb.innerHTML = ""; return; }
  const linked = S.docLink && S.docLink.series_id === S.sid;
  const chOpts = `<option value="">(no chapter)</option>` + [...S.b.chapters]
    .sort((a, b) => (a.number || 0) - (b.number || 0))
    .map((c) => `<option value="${c.id}" ${linked && S.docLink.chapter_id === c.id ? "selected" : ""}>
      Ch ${esc(c.number)} – ${esc(c.title)}</option>`).join("");
  lb.innerHTML = linked
    ? `This doc: <select data-act="doc-chapter">${chOpts}</select>`
    : `<span>This document isn't linked.</span><button class="small" data-act="doc-link">Link to this series</button>`;
}

function render() {
  renderHeader();
  const m = $("#main");
  // Checked before the "no series" empty state below, not after: feedback
  // doesn't need a series to exist (it isn't tied to S.b at all), and if
  // it were gated behind having one, a user hitting a bug that prevents
  // creating their first series could never report that exact bug.
  if (S.view?.kind === "feedback") { m.innerHTML = renderForm(); return; }
  if (!S.b) {
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
    return;
  }
  m.innerHTML = ({ characters: listCharacters, locations: listLocations,
    events: listEvents, research: listResearch, series: seriesForm })[S.tab]();
}

function toolbar(kind, placeholder, extra = "") {
  return `<div class="toolbar"><input data-act="filter" placeholder="${placeholder}" value="${esc(S.filter)}">
    ${extra}<button class="primary" data-act="new" data-kind="${kind}">+ Add</button></div>`;
}
function matches(r) {
  if (!S.filter) return true;
  return JSON.stringify(r).toLowerCase().includes(S.filter.toLowerCase());
}

function listCharacters() {
  const items = S.b.characters.filter(matches).sort(byName);
  return toolbar("characters", "Search characters…") + (items.length
    ? `<ul class="list">${items.map((c) => `<li data-act="open" data-kind="characters" data-id="${c.id}">
        <div class="title">${esc(c.name)} ${c.role ? `<span class="sub">· ${esc(c.role)}</span>` : ""}</div>
        <div class="sub">${esc([c.age && c.age + " yo", c.hair, c.eyes, c.style].filter(Boolean).join(" · "))}</div>
        <div class="sub">${relsFor(c.id).slice(0, 3).map((r) => esc(relSentence(r, c.id))).join("; ")}</div>
      </li>`).join("")}</ul>`
    : `<div class="empty">No characters yet.</div>`);
}

function listLocations() {
  const items = S.b.locations.filter(matches).sort(byName);
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
  const cOpts = `<option value="">All characters</option>` + [...S.b.characters].sort(byName)
    .map((c) => `<option value="${c.id}" ${S.tlChar === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  const items = S.b.events.filter(matches)
    .filter((e) => !S.tlChapter || e.chapter_id === S.tlChapter)
    .filter((e) => !S.tlChar || (e.character_ids || []).includes(S.tlChar))
    .sort((a, b) => sortKey(a) - sortKey(b));
  const anchor = s.anchor_mode === "date" && s.anchor_date
    ? `Anchored at <b>${esc(s.anchor_date)}</b>` : `Relative timeline from <b>${esc(s.anchor_label || "Story start")}</b>`;
  return `<div class="hint">${anchor} · change in Series tab</div>` +
    toolbar("events", "Search events…") +
    `<div class="toolbar"><select data-act="tl-chapter">${chOpts}</select><select data-act="tl-char">${cOpts}</select></div>` +
    (items.length ? `<ul class="list tl">${items.map((e) => {
      const ages = (e.character_ids || []).map((id) => rec("characters", id)).filter(Boolean)
        .map((c) => { const a = ageAt(c, e); return `${c.name}${a !== null ? " " + a : ""}`; });
      const ch = rec("chapters", e.chapter_id), loc = rec("locations", e.location_id);
      return `<li data-act="open" data-kind="events" data-id="${e.id}">
        <div class="when">${esc(whenLabel(e))}</div>
        <div class="title">${esc(e.title)}</div>
        <div class="sub">${esc([ch && "Ch " + ch.number, loc && loc.name].filter(Boolean).join(" · "))}</div>
        ${ages.length ? `<div class="ages">${esc(ages.join(" · "))}</div>` : ""}
      </li>`; }).join("")}</ul>`
      : `<div class="empty">No events yet.</div>`);
}

function todayIso() { return new Date().toISOString().slice(0, 10); }
function textPreview(html, max = 140) {
  // <template>.content is an inert DocumentFragment - unlike a plain <div>,
  // setting innerHTML here never fetches/decodes any <img> the body has,
  // even briefly, since it's never part of the render tree.
  const tpl = document.createElement("template"); tpl.innerHTML = html || "";
  const t = (tpl.content.textContent || "").replace(/\s+/g, " ").trim();
  return t.length > max ? t.slice(0, max) + "…" : t;
}

function listResearch() {
  const sortOpts = [["date_desc", "Newest first"], ["date_asc", "Oldest first"], ["title", "Title A–Z"]]
    .map(([v, label]) => `<option value="${v}" ${S.rsSort === v ? "selected" : ""}>${label}</option>`).join("");
  const items = S.b.research.filter(matches).sort((a, b) => {
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

function characterForm(c, isNew) {
  const tmpl = S.b.series.character_fields || [];
  const custom = c.custom || {};
  const keys = [...tmpl, ...Object.keys(custom).filter((k) => !tmpl.includes(k))];
  const others = S.b.characters.filter((x) => x.id !== c.id).sort(byName);
  const evs = S.b.events.filter((e) => (e.character_ids || []).includes(c.id))
    .sort((a, b) => sortKey(a) - sortKey(b));
  return `<form data-kind="characters">
    <h2>${isNew ? "New character" : esc(c.name)}</h2>
    ${S.inWord && !isNew ? `<div class="toolbar"><button type="button" class="small" data-act="insert" data-text="${esc(c.name)}">Insert name at cursor</button></div>` : ""}
    <div class="grid2">${field("name", "Name", c.name, "text", "", true)}${field("role", "Role", c.role, "text", 'placeholder="e.g. Protagonist"')}</div>
    ${field("aliases", "Nicknames / aliases (comma separated)", c.aliases)}
    <h3>Basics</h3>
    <div class="grid3">${field("age", "Age at start", c.age, "number")}${field("height", "Height", c.height)}${field("gender", "Gender", c.gender)}</div>
    <div class="grid3">${field("hair", "Hair", c.hair)}${field("eyes", "Eyes", c.eyes)}${field("style", "Style", c.style, "text", 'placeholder="goth, natural…"')}</div>
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
    <datalist id="relTypes">${REL_TYPES.map((t) => `<option value="${t}">`).join("")}</datalist>
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
    <div class="formbar"><div><button type="button" class="danger" data-act="delete-series">Delete series</button></div>
      <div><button class="primary" data-act="save" data-kind="series">Save</button></div></div>
  </form>
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
  ${S.config?.authMode === "token" ? `
  <h3>Connection</h3>
  <label><span>API token (only if the server sets STORYBIBLE_TOKEN)</span>
    <input id="tokenInput" type="password" value="${esc(lsGet("sb_token"))}"></label>
  <button type="button" data-act="save-token">Save token</button>` : ""}`;
}

function feedbackForm(kind) {
  const label = kind === "issue" ? "Log an issue" : "Log a suggestion";
  return `<form data-kind="feedback" data-feedback-kind="${kind}">
    <h2>${label}</h2>
    ${field("title", "Title", "", "text", 'maxlength="120" placeholder="Short summary"', true)}
    <div class="hint">3–120 characters.</div>
    ${field("description", "Description", "", "textarea", 'rows="6" maxlength="4000" placeholder="What happened, or what you\'d like to see"', true)}
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
  if (["save", "chip", "add-field", "add-rel", "del-rel", "delete", "delete-series",
       "add-chapter", "del-chapter", "cancel", "insert", "export", "import-pick", "save-token",
       "log-issue", "log-suggestion", "save-feedback", "sign-in", "sign-out"].includes(act)) ev.preventDefault();
  try {
    switch (act) {
      case "open": S.view = { kind: t.dataset.kind, id: t.dataset.id }; render(); window.scrollTo(0, 0); break;
      case "new": S.view = { kind: t.dataset.kind, id: null }; render(); break;
      case "cancel": S.view = null; render(); break;
      case "chip": t.classList.toggle("on"); break;
      case "new-series": await newSeries(); break;
      case "save": await save(t.dataset.kind, t.closest("form")); break;
      case "delete": {
        if (!t.classList.contains("armed")) { t.classList.add("armed"); t.textContent = "Confirm delete"; return; }
        await api(`/series/${S.sid}/${t.dataset.kind}/${S.view.id}`, "DELETE");
        S.view = null; await loadBundle(); render(); toast("Deleted"); break;
      }
      case "delete-series": {
        if (!t.classList.contains("armed")) { t.classList.add("armed"); t.textContent = "Delete EVERYTHING in this series?"; return; }
        await api(`/series/${S.sid}`, "DELETE"); await loadSeriesList();
        await selectSeries(S.seriesList[0]?.id); toast("Series deleted"); break;
      }
      case "add-field": {
        const name = $("#newFieldName").value.trim(); if (!name) return;
        $("#customFields").insertAdjacentHTML("beforeend",
          `<span class="hint" style="margin:6px 0 0">${esc(name)}</span><input data-custom="${esc(name)}"><span></span>`);
        $("#newFieldName").value = ""; break;
      }
      case "add-rel": {
        const type = $("#relType").value.trim(), to = $("#relTo").value;
        if (!type || !to) { toast("Pick a type and a character"); return; }
        await api(`/series/${S.sid}/relationships`, "POST",
          { data: { from: S.view.id, to, type, note: $("#relNote").value.trim() } });
        await refreshKeepForm(); break;
      }
      case "del-rel": await api(`/series/${S.sid}/relationships/${t.dataset.id}`, "DELETE"); await refreshKeepForm(); break;
      case "add-chapter": {
        const title = $("#chTitle").value.trim(); if (!title) return;
        await api(`/series/${S.sid}/chapters`, "POST", { data: { number: Number($("#chNum").value) || 0, title } });
        await loadBundle(); render(); break;
      }
      case "del-chapter": await api(`/series/${S.sid}/chapters/${t.dataset.id}`, "DELETE"); await loadBundle(); render(); break;
      case "doc-link": saveDocLink({ series_id: S.sid, chapter_id: "" }); render(); break;
      case "insert": await insertText(t.dataset.text); break;
      case "export": {
        const blob = new Blob([JSON.stringify(S.b, null, 2)], { type: "application/json" });
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
        a.download = `${S.b.series.name.replace(/[^\w-]+/g, "_")}.storybible.json`; a.click(); break;
      }
      case "import-pick": $("#importFile").click(); break;
      case "save-token": lsSet("sb_token", $("#tokenInput").value.trim()); await boot(); toast("Token saved"); break;
      case "sign-in": await signIn(); if (msalAccount) { await loadApp(); } else { renderHeader(); } break;
      case "sign-out": await signOut(); S.b = null; S.seriesList = []; render(); break;
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
  } catch (err) { toast(err.message); console.error(err); }
}

async function refreshKeepForm() {
  const v = S.view; await loadBundle(); S.view = v; render();
}

async function newSeries() {
  const s = await api("/series", "POST", { data: { name: "New series", anchor_mode: "relative",
    anchor_label: "Story start", character_fields: DEFAULT_FIELDS } });
  await loadSeriesList(); S.tab = "series"; await selectSeries(s.id); toast("Series created — name it here");
}

async function save(kind, form) {
  const data = readForm(form);
  if (kind === "series") {
    // #12: PUT must send back the version this was loaded at, so a save
    // from a stale copy (someone else changed it meanwhile) 409s instead
    // of silently overwriting their edit.
    await api(`/series/${S.sid}`, "PUT", { data: { ...S.b.series, ...data }, version: S.b.series.version });
    await loadSeriesList(); await loadBundle(); render(); toast("Saved"); return;
  }
  if ((kind === "characters" || kind === "locations") && !data.name?.trim()) { toast("Name is required"); return; }
  if ((kind === "events" || kind === "research") && !data.title?.trim()) {
    toast(kind === "events" ? "Say what happens" : "Title is required"); return;
  }
  if (S.view.id) {
    const prev = rec(kind, S.view.id);
    await api(`/series/${S.sid}/${kind}/${S.view.id}`, "PUT", { data: { ...prev, ...data }, version: prev.version });
    await refreshKeepForm();
  } else {
    const r = await api(`/series/${S.sid}/${kind}`, "POST", { data });
    await loadBundle(); S.view = kind === "characters" ? { kind, id: r.id } : null; render();
  }
  toast("Saved");
}

async function findSelection() {
  const txt = (await wordSelection()).toLowerCase();
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
      if (t.value === "__new") await newSeries(); else await selectSeries(t.value);
    }
    if (t.dataset.act === "tl-chapter") { S.tlChapter = t.value; render(); }
    if (t.dataset.act === "tl-char") { S.tlChar = t.value; render(); }
    if (t.dataset.act === "research-sort") { S.rsSort = t.value; render(); }
    if (t.dataset.act === "doc-chapter") saveDocLink({ series_id: S.sid, chapter_id: t.value });
    if (t.id === "importFile" && t.files[0]) {
      try {
        const r = await api("/import", "POST", JSON.parse(await t.files[0].text()));
        await loadSeriesList(); await selectSeries(r.id); toast("Imported");
      } catch (err) { toast("Import failed: " + err.message); }
    }
  });
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
    S.tab = b.dataset.tab; S.view = null; S.filter = ""; render();
  }));
  $("#btnFind").addEventListener("click", () => findSelection().catch((e) => toast(e.message)));
}

async function loadApp() {
  try {
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

async function boot() {
  await initAuth();
  if (S.config.authMode === "entra" && !msalAccount) {
    // No cached account - MSAL popups need a user gesture anyway (a popup
    // opened outside a click handler is just blocked), so this only ever
    // shows a Sign in button, never auto-prompts.
    renderHeader();
    $("#main").innerHTML = `<div class="empty">Sign in to continue.<br><br>
      <button class="primary" data-act="sign-in">Sign in</button></div>`;
    return;
  }
  await loadApp();
}

function start(info) {
  S.inWord = !!(info && info.host === Office.HostType.Word);
  document.body.classList.toggle("in-word", S.inWord);
  wire(); boot();
}

if (window.Office && Office.onReady) Office.onReady(start);
else document.addEventListener("DOMContentLoaded", () => start(null));
