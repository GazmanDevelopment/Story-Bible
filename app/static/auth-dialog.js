/* Loaded by auth-dialog.html - a separate file rather than an inline script so
 * the Content-Security-Policy can forbid inline scripts (#69). */
/* Only used by Word hosts without nested app auth support (#13) -
 * everywhere else signs in directly in the task pane. This page runs
 * in Office's dialog host, which behaves like a normal browser tab, so
 * the ordinary MSAL redirect flow works: navigate to Entra, come back
 * here, then hand the resulting account off to the task pane (which
 * shares this same origin's localStorage MSAL cache) and close.
 */
// Privacy / terms links so they are visible before first sign-in (#91). Built
// with DOM calls (no innerHTML); only https URLs are used. The dialog would
// otherwise redirect to Entra straight away, so when there are links to show
// this waits for a Continue click; with none configured it resolves at once.
function confirmAfterLegalLinks(cfg) {
  const box = document.getElementById("legal");
  if (!box) return Promise.resolve();
  [["Privacy policy", cfg.privacyUrl], ["Terms of use", cfg.termsUrl]].forEach(([label, url]) => {
    if (!/^https:\/\//i.test(url || "")) return;
    if (box.childNodes.length) box.append(" · ");
    const a = document.createElement("a");
    a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = label;
    box.append(a);
  });
  if (!box.childNodes.length) return Promise.resolve();
  return new Promise((resolve) => {
    const btn = document.createElement("button");
    btn.id = "continue"; btn.textContent = "Continue to sign in";
    btn.addEventListener("click", () => { btn.disabled = true; resolve(); });
    box.after(btn);
  });
}

async function run() {
  let cfg;
  try {
    cfg = await (await fetch("/api/config")).json();
  } catch (err) {
    Office.context.ui.messageParent(JSON.stringify({ ok: false, error: "Couldn't reach the server" }));
    return;
  }
  const pca = new msal.PublicClientApplication({
    auth: {
      clientId: cfg.clientId,
      // From the server (#91): home tenant, or "common" in open signup.
      authority: cfg.authority || `https://login.microsoftonline.com/${cfg.tenantId}`,
      redirectUri: window.location.origin + "/auth-dialog.html",
    },
    cache: { cacheLocation: "localStorage" },
  });
  await pca.initialize();
  try {
    const result = await pca.handleRedirectPromise();
    if (result) {
      Office.context.ui.messageParent(JSON.stringify({ ok: true }));
      return;
    }
    await confirmAfterLegalLinks(cfg);
    await pca.loginRedirect({ scopes: [`api://${cfg.clientId}/access_as_user`] });
    // loginRedirect navigates away; nothing runs past this line until
    // the browser comes back here and handleRedirectPromise() (above)
    // picks up the result on the next load.
  } catch (err) {
    Office.context.ui.messageParent(JSON.stringify({ ok: false, error: String(err && err.message || err) }));
  }
}
// Same defensive pattern as app.js: this page only does anything useful
// as a real Office dialog (messageParent has no one to message
// otherwise), but it shouldn't throw an unhandled error if opened
// without Office.js available (e.g. loaded directly while debugging).
if (window.Office && Office.onReady) Office.onReady(run);
else document.body.textContent = "This page must be opened as an Office dialog.";
