// Chronicle Export — background script.
// Thin relay. Re-injects the content script into open claude.ai tabs on
// install/update so the popup can talk to it immediately after a reload.
// Holds no state, so it runs as a Chrome service worker or a Firefox event page.

// Firefox loads session.js from the manifest's background scripts; a Chrome
// service worker takes one file, so it imports it here.
if (typeof importScripts === 'function') importScripts('session.js');

function injectContentScript(tabId, callback) {
  chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] }, callback);
}

// When injection failed because the user took claude.ai access away, say where
// to give it back instead of the browser's bare "Missing host permission".
function explainInjectError(message, callback) {
  hasClaudeAccess().then((granted) => callback(granted ? message : NO_CLAUDE_ACCESS));
}

chrome.runtime.onInstalled.addListener(() => {
  console.log('[Chronicle] background installed');
  chrome.tabs.query({ url: 'https://claude.ai/*' }, (tabs) => {
    tabs.forEach((tab) => {
      injectContentScript(tab.id, () => {
        if (chrome.runtime.lastError) {
          console.log('[Chronicle] could not inject into tab', tab.id, chrome.runtime.lastError.message);
        }
      });
    });
  });
});

// Popup → background: "make sure the content script is injected in the active tab"
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action !== 'ensureContentScript') return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs[0];
    if (!tab) {
      sendResponse({ success: false, error: 'No active tab. Click into a browser window, then click Fetch again.' });
      return;
    }
    if (!tab.url || !tab.url.includes('claude.ai')) {
      // Fall back to any claude.ai tab if the active one isn't claude.
      chrome.tabs.query({ url: 'https://claude.ai/*' }, (claudeTabs) => {
        if (!claudeTabs || claudeTabs.length === 0) {
          sendResponse({ success: false, error: 'No claude.ai tab open. Open https://claude.ai and log in, then click Fetch again.' });
          return;
        }
        const target = claudeTabs[0];
        injectContentScript(target.id, () => {
          if (chrome.runtime.lastError) {
            explainInjectError(chrome.runtime.lastError.message, (error) => sendResponse({ success: false, error }));
          } else {
            sendResponse({ success: true, tabId: target.id });
          }
        });
      });
      return;
    }
    injectContentScript(tab.id, () => {
      if (chrome.runtime.lastError) {
        explainInjectError(chrome.runtime.lastError.message, (error) => sendResponse({ success: false, error }));
      } else {
        sendResponse({ success: true, tabId: tab.id });
      }
    });
  });
  return true;
});
