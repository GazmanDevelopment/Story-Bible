/**
 * Story Bible - Google Docs spike (#152).
 *
 * Throwaway: proves the existing pane can run in a Docs sidebar iframe and
 * sign in with Microsoft. No document access yet; that is the follow-up
 * (#153).
 */

// The server that hosts the pane. Change this to test against another one.
var SERVER_URL = 'https://storybible.huscroft.com.au';

function onOpen() {
  DocumentApp.getUi()
    .createMenu('Story Bible')
    .addItem('Open sidebar', 'showSidebar')
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
