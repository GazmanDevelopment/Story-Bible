/**
 * Story Bible - Google Docs add-on (server side, runs inside the Doc).
 *
 * The sidebar (Sidebar.html) frames the Story Bible pane and relays its
 * requests to the functions below. Only these four are reachable from the
 * pane, via the ROUTES table in Sidebar.html. Helpers end in "_" so Apps
 * Script doesn't expose them to google.script.run.
 */

// The server that hosts the pane. Change this to use another one.
var SERVER_URL = 'https://storybible.huscroft.com.au';

// Document property the series/chapter link is stored under (same key the
// Word add-in uses in the document's settings).
var LINK_KEY = 'storybible';

// A selection longer than this is never a name; don't ship a whole chapter.
var MAX_SELECTION_CHARS = 500;

function onOpen() {
  DocumentApp.getUi()
    .createMenu('Story Bible')
    .addItem('Open sidebar', 'showSidebar')
    .addItem('Open in New Tab', 'openInNewTab')
    .addToUi();
}

function onInstall() {
  onOpen();
}

function showSidebar() {
  var tpl = HtmlService.createTemplateFromFile('Sidebar');
  tpl.serverUrl = SERVER_URL;
  var page = tpl.evaluate().setTitle('Story Bible');
  DocumentApp.getUi().showSidebar(page);
}

/**
 * Open Story Bible in a browser tab of its own (the plain web version: no
 * Find or Insert, since it has no document beside it). Apps Script can't open
 * a tab directly, so show a small dialog that opens it and offers a link in
 * case the browser blocks that.
 */
function openInNewTab() {
  var tpl = HtmlService.createTemplateFromFile('NewTab');
  tpl.serverUrl = SERVER_URL;
  DocumentApp.getUi().showModalDialog(tpl.evaluate().setWidth(320).setHeight(130), 'Story Bible');
}

/** The selected text ('' if nothing is selected, or only the cursor is). */
function getSelectionText() {
  var sel = DocumentApp.getActiveDocument().getSelection();
  if (!sel) return '';
  var parts = [];
  sel.getRangeElements().forEach(function (re) {
    var text = textElement_(re);
    if (!text) return;
    var s = text.getText();
    if (re.isPartial()) s = s.substring(re.getStartOffset(), re.getEndOffsetInclusive() + 1);
    parts.push(s);
  });
  return parts.join(' ').substring(0, MAX_SELECTION_CHARS);
}

/**
 * Replace the selection with `text`, or insert it at the cursor when nothing
 * is selected. A selection spanning several paragraphs is cleared and the
 * text goes where it started (the emptied paragraphs are left in place).
 */
function insertAtCursor(text) {
  text = String(text);
  var doc = DocumentApp.getActiveDocument();
  var sel = doc.getSelection();

  if (sel) {
    var first = null;   // {text, start}: where the replacement goes
    sel.getRangeElements().forEach(function (re) {
      var t = textElement_(re);
      if (!t || t.getText().length === 0) return;
      var start = re.isPartial() ? re.getStartOffset() : 0;
      var end = re.isPartial() ? re.getEndOffsetInclusive() : t.getText().length - 1;
      if (!first) first = { text: t, start: start };
      t.deleteText(start, end);
    });
    if (first) {
      first.text.insertText(first.start, text);
      return true;
    }
    // A selection with no text in it (e.g. only an image): fall through to the cursor.
  }

  var cursor = doc.getCursor();
  if (!cursor) throw new Error('Click in the document where the name should go.');
  if (!cursor.insertText(text)) throw new Error("Can't insert text at the cursor here.");
  return true;
}

/** The stored link as a JSON string ('{"series_id":..,"chapter_id":..}'), or null. */
function getDocLink() {
  return PropertiesService.getDocumentProperties().getProperty(LINK_KEY);
}

/** Store the series/chapter link (a JSON string) for this document. */
function setDocLink(linkJson) {
  var link = JSON.parse(linkJson);
  if (!link || typeof link.series_id !== 'string' || typeof link.chapter_id !== 'string') {
    throw new Error('Invalid document link');
  }
  PropertiesService.getDocumentProperties().setProperty(
    LINK_KEY,
    JSON.stringify({ series_id: link.series_id, chapter_id: link.chapter_id })
  );
}

/**
 * The Text of a RangeElement, or null if it has none. A selection that covers
 * a whole paragraph comes back as the Paragraph, not a Text, so accept any
 * element that can give its text.
 */
function textElement_(re) {
  var el = re.getElement();
  return typeof el.asText === 'function' ? el.asText() : null;
}
