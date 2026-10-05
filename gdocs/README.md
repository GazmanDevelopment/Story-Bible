# Story Bible for Google Docs

An Apps Script add-on that puts the Story Bible pane in a Google Docs sidebar.
The sidebar is only a thin shell: it frames the existing pane
(`/gdocs.html` on your server) and relays the pane's requests (read the
selection, insert a name, remember which series/chapter a Doc is linked to) to
Apps Script functions. Sign-in is the same Microsoft sign-in as everywhere
else; nothing about the server's auth changes.

| File | What it is |
|------|------------|
| [Code.gs](Code.gs) | Menu, and the four functions the pane can reach: `getSelectionText`, `insertAtCursor`, `getDocLink`, `setDocLink`. `SERVER_URL` is set here. |
| [Sidebar.html](Sidebar.html) | The iframe shell. Only answers our own pane, and only the four `ROUTES` in it. |
| [NewTab.html](NewTab.html) | The small dialog behind **Open in New Tab**. |
| [appsscript.json](appsscript.json) | Manifest with the two minimal scopes. |

## Server prerequisites

- The server must serve `/gdocs.html`. It is `index.html` without the Office.js
  tag: Office.js blanks any page it finds itself framed in, so the sidebar can't
  frame `index.html`.
- The Entra app registration needs `https://<your server>/gdocs.html` as a
  redirect URI under the **Single-page application** platform (MSAL uses the
  URL of the page that starts sign-in). See PLAN.md section 3.

## Install (private / test deployment)

**The script belongs to one Doc, not to your Google account.** It is stored
with the Doc it was added to (a "container-bound" script). A new, blank Doc, a
different Google account or a different browser profile starts without it. So
each manuscript Doc needs the script added once, and the easiest ways are:

1. **Make a copy of a Doc that already has it** (File > Make a copy). The copy
   keeps the script, so keep one good Doc as a template. Authorise it once
   again on first use.
2. **`clasp`** pushes the files from this folder into a Doc in one command
   (below). Best for adding it to existing Docs.
3. **Paste the files by hand** in the Apps Script editor (below). Fine for a
   one-off.

Whichever you use, if your server isn't `https://storybible.huscroft.com.au`,
edit `SERVER_URL` in `Code.gs` first.

Having it in every Doc without doing any of this needs a published add-on
(a Google Workspace Marketplace listing, which can be unlisted or private).
That is not done yet.

### With clasp

One-off setup (needs Node 22 or later):

1. `npm install -g @google/clasp`
2. Switch on the **Google Apps Script API** at
   <https://script.google.com/home/usersettings>.
3. `clasp login` and sign in with the Google account that owns the Docs.

For each Doc, from this `gdocs/` folder, using the Doc's ID (the long string in
its address, `docs.google.com/document/d/<DOC_ID>/edit`):

```
clasp create-script --type docs --title "Story Bible" --parentId <DOC_ID>
clasp push -f
```

`create-script` makes a script bound to that Doc and writes a `.clasp.json`
here (it is git-ignored: it names one Doc's script). `push -f` uploads
`Code.gs`, `Sidebar.html`, `NewTab.html` and `appsscript.json`, and overwrites
the manifest. To add the script to another Doc, delete `.clasp.json` and run the
two commands again with that Doc's ID. After changing a file here, `clasp push -f`
updates the Doc you last set up.

Then reload the Doc and use **Story Bible > Open sidebar**. Accept the one-time
authorisation prompt ("Google hasn't verified this app" is expected for a
personal script: Advanced > Go to project).

### By hand

1. Create or open a Google Doc.
2. **Extensions > Apps Script**.
3. Replace the contents of `Code.gs` with [Code.gs](Code.gs).
4. **+ > HTML**, create `Sidebar` and paste [Sidebar.html](Sidebar.html). Do the
   same for `NewTab` and [NewTab.html](NewTab.html). (Names are case-sensitive and
   have no extension.)
5. Project Settings (gear) > tick **Show "appsscript.json" manifest file in
   editor**, then replace its contents with [appsscript.json](appsscript.json).
6. **Save** (Ctrl+S) in the editor; unsaved files are lost if you close the tab.
7. Reload the Doc, then use **Story Bible > Open sidebar** and accept the
   authorisation prompt as above.

## The menu

The **Story Bible** menu has two items: **Open sidebar**, and **Open in New Tab**
for the plain web version in a tab of its own (no Find or Insert there, since
there's no document beside it).

## Using it

- **Find:** select a name in the Doc and click Find. The pane jumps to that
  character or place.
- **Insert name at cursor:** open a character and click "Insert name at cursor".
  It replaces the selection, or goes at the cursor if nothing is selected.
- **Link this Doc:** link the Doc to a series, and optionally a chapter. The link
  is stored in the Doc's properties, so it stays with the Doc for everyone who
  uses the add-on on it.

Known limits: replacing a selection that spans several paragraphs clears the
selected text and puts the new text at the start, leaving the emptied
paragraphs in place. Only text is read from a selection (images and the like
are skipped).

## Manual checks after a change

The browser tests cover the pane and the shell with Apps Script stubbed. These
need a real Doc:

| # | Check |
|---|-------|
| 1 | Find with a name selected, with a multi-word name, and with only a cursor (should say to select a name first) |
| 2 | Insert replaces a selected word; inserts at the cursor when nothing is selected |
| 3 | Insert while a whole paragraph is selected |
| 4 | Link the Doc, close and reopen the sidebar: the link is remembered |
| 5 | Reload the Doc and a fresh browser session: still linked |
| 6 | **Open in New Tab** opens the pane in a tab (or offers the link if blocked) |
| 7 | Repeat 1, 2 and 4 in Firefox and Safari (nested-iframe storage is stricter there) |

## Spike findings (for the record)

- Office.js must not load in the framed page: it blanks it. Hence `gdocs.html`.
- Microsoft's popup sign-in works inside the sidebar, and the sign-in survives
  closing and reopening it.
