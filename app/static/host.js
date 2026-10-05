/* Story Bible - host abstraction.
 *
 * The pane runs in a few places: inside Word (Office.js), in a Google Docs
 * sidebar, and as a plain web page. Everything that touches "the document the
 * pane is open beside" lives here, behind one small interface, so app.js never
 * calls Office.js (or the Docs shell) directly and another host is just
 * another entry below.
 *
 *   host.name              "word" | "gdocs" | "web"
 *   host.hasDocument       true when there is a document to read/write
 *   host.init()            detect the host; resolves once it is ready
 *   host.nestedAuth()      can MSAL sign in with a normal popup / NAA here?
 *   host.openAuthDialog()  sign-in fallback when it can't (old Word only)
 *   host.readDocLink()     -> {series_id, chapter_id} | null
 *   host.saveDocLink(link) -> Promise, resolves once persisted
 *   host.getSelection()    -> trimmed selected text
 *   host.insertText(text)  replaces the selection
 *   host.openExternal(url) opens url in the system browser
 *
 * Plain JS, no build step, no innerHTML.
 */
(function () {
  const DOC_KEY = "storybible";   // key the link is stored under in the document

  function openInTab(url) { window.open(url, "_blank", "noopener"); }

  // Web (a normal browser tab): no document, so the document-only controls
  // stay hidden (see body.has-doc in styles.css) and these are inert.
  const web = {
    name: "web",
    hasDocument: false,
    nestedAuth: () => true,   // standard browser SPA flow, same as MSAL's nestable app outside Word
    openAuthDialog: () => Promise.reject(new Error("No sign-in dialog outside Word")),
    readDocLink: async () => null,
    saveDocLink: async () => {},
    getSelection: async () => "",
    insertText: async () => {},
    openExternal: openInTab,
  };

  // Word, via Office.js.
  const word = {
    name: "word",
    hasDocument: true,
    nestedAuth: () => !!window.Office?.context?.requirements?.isSetSupported?.("NestedAppAuth", "1.1"),

    // Perpetual Office without NAA can't do a normal popup from the task pane,
    // so the interactive part happens in a separate Office dialog (a small
    // same-origin page, auth-dialog.html) running the standard redirect flow.
    // This only reports success; the caller re-reads the account both pages
    // share via the same localStorage cache.
    openAuthDialog() {
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
    },

    async readDocLink() {
      return Office.context.document.settings.get(DOC_KEY) || null;
    },
    saveDocLink(link) {
      return new Promise((resolve, reject) => {
        Office.context.document.settings.set(DOC_KEY, link);
        Office.context.document.settings.saveAsync((res) => {
          if (res && res.status === Office.AsyncResultStatus.Failed) reject(new Error(res.error?.message || "Couldn't save the document link"));
          else resolve();
        });
      });
    },
    getSelection() {
      return Word.run(async (ctx) => {
        const r = ctx.document.getSelection(); r.load("text"); await ctx.sync();
        return (r.text || "").trim();
      });
    },
    async insertText(text) {
      await Word.run(async (ctx) => {
        ctx.document.getSelection().insertText(text, "Replace"); await ctx.sync();
      });
    },
    // A plain target=_blank link is unreliable in the Word task pane, so ask
    // Office to open the system browser there.
    openExternal(url) {
      if (window.Office?.context?.ui?.openBrowserWindow) Office.context.ui.openBrowserWindow(url);
      else openInTab(url);
    },
  };

  // Google Docs sidebar. The pane (gdocs.html) is framed by an Apps Script
  // page (gdocs/Sidebar.html) which owns the document access; this asks it to
  // act by postMessage and waits for the reply. The shell only answers the
  // fixed set of message types below, and only for our own origin.
  const CALL_TIMEOUT_MS = 15000;
  const pending = new Map();   // id -> {resolve, reject, timer}
  let nextId = 1;

  function call(type, args) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("The document didn't respond"));
      }, CALL_TIMEOUT_MS);
      pending.set(id, { resolve, reject, timer });
      // The shell's origin isn't fixed (Apps Script serves it from a generated
      // googleusercontent.com host), so the target is "*". Only names and the
      // series/chapter link ever go this way; replies are accepted only from
      // our direct parent window (below).
      window.parent.postMessage({ sb: 1, id, type, args: args || {} }, "*");
    });
  }

  window.addEventListener("message", (e) => {
    if (e.source !== window.parent) return;
    const m = e.data;
    if (!m || m.sb !== 1 || !pending.has(m.id)) return;
    const p = pending.get(m.id);
    pending.delete(m.id);
    clearTimeout(p.timer);
    if (m.ok) p.resolve(m.result);
    else p.reject(new Error(m.error || "The document request failed"));
  });

  const gdocs = Object.assign({}, web, {
    name: "gdocs",
    hasDocument: true,
    async readDocLink() {
      const raw = await call("readDocLink");
      if (!raw) return null;
      try { return JSON.parse(raw); } catch { return null; }
    },
    async saveDocLink(link) { await call("saveDocLink", { link: JSON.stringify(link) }); },
    async getSelection() { return String((await call("getSelection")) || "").trim(); },
    async insertText(text) { await call("insertText", { text: String(text) }); },
  });

  const host = Object.assign({}, web);
  host.init = async function init() {
    // gdocs.html marks itself, and only counts when it really is framed
    // (opened directly in a tab it is just the web pane).
    if (document.documentElement.dataset.host === "gdocs" && window.parent !== window) {
      Object.assign(host, gdocs);
      return;
    }
    // office.js is loaded on every page; outside Word it still calls back,
    // just with a different host, so only a Word host swaps the word impl in.
    if (window.Office && Office.onReady) {
      const info = await new Promise((resolve) => Office.onReady(resolve));
      if (info && info.host === Office.HostType.Word) Object.assign(host, word);
    }
  };
  window.host = host;
})();
