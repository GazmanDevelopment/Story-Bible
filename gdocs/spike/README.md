# Google Docs spike (#152)

A throwaway Apps Script project that puts the existing Story Bible pane in a
Google Docs sidebar iframe. It answers one question: **can the pane load there
and sign in with Microsoft?** It does not read or write the document yet.

## Prerequisite: the server must serve `/gdocs.html`

The sidebar frames `/gdocs.html`, a copy of the pane's page without the
Office.js script tag. Office.js blanks any page it finds itself framed in
(it logs "The add-in is not hosted in plain browser top window" and the frame
goes to `about:blank`), which is what the first attempt at this spike hit:
a brief flash of background colour, then a blank sidebar. So deploy a server
build that includes `gdocs.html` before testing.

## Install (about 5 minutes)

1. Create a new Google Doc (any Google account).
2. **Extensions > Apps Script**. This opens a script bound to that Doc.
3. Replace the contents of `Code.gs` with [Code.gs](Code.gs).
4. **+ > HTML**, name it `Sidebar` (no extension), and paste [Sidebar.html](Sidebar.html).
5. Project Settings (gear) > tick **Show "appsscript.json" manifest file in
   editor**, then replace its contents with [appsscript.json](appsscript.json).
6. If your server isn't `https://storybible.huscroft.com.au`, edit `SERVER_URL`
   in `Code.gs`.
7. Save, reload the Doc, and wait a few seconds for the **Story Bible** menu.
   Choose **Story Bible > Open sidebar** and accept the one-time
   authorisation prompt ("Google hasn't verified this app" is expected for a
   personal script: Advanced > Go to project).

## What to check (Chrome, Firefox, Safari if you have it)

Record Pass / Fail / Notes for each browser.

| # | Check | How |
|---|-------|-----|
| 1 | Pane loads in the sidebar | Sidebar shows the Story Bible UI, not a blank panel or the "hasn't loaded" hint |
| 2 | Sign-in popup opens | Click **Sign in**. A Microsoft popup should open (not be blocked) |
| 3 | Sign-in completes | Finish sign-in. Series list appears |
| 4 | API calls work | Open a series, switch tabs, save an edit |
| 5 | Sign-in survives closing the sidebar | Close the sidebar, reopen it from the menu: still signed in |
| 6 | Sign-in survives reloading the Doc | Reload the browser tab, reopen the sidebar: still signed in |
| 7 | Sign-in survives a new session | Fully close the browser, reopen the Doc and sidebar: still signed in |

Useful when something fails: open the browser dev tools console and look for
errors from the iframe (frame blocked, popup blocked, storage access denied).
Safari and Firefox are the most likely to block storage in a nested iframe.

## Results

| Browser | 1 | 2 | 3 | 4 | 5 | 6 | 7 | Notes |
|---------|---|---|---|---|---|---|---|-------|
| Chrome  |   |   |   |   |   |   |   |       |
| Firefox |   |   |   |   |   |   |   |       |
| Safari  |   |   |   |   |   |   |   |       |

## If it fails

- **Storage blocked (check 5/6/7 fail):** keep tokens in memory and hold them in
  the Apps Script shell (`PropertiesService.getUserProperties()`), passing them
  to the iframe by `postMessage`.
- **Popup blocked (check 2 fails):** open sign-in in a new tab and hand the
  token back through the shell.
- **Frame refused (check 1 fails):** the server sends no `X-Frame-Options` or
  `frame-ancestors`, so look at the Apps Script page's own restrictions and the
  browser console before changing the server.
