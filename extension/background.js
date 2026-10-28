// Chronicle Export — background script.
// Thin relay. Re-injects the content script into open claude.ai tabs on
// install/update so the popup can talk to it immediately after a reload.
// Holds no state, so it runs as a Chrome service worker or a Firefox event page.

// Firefox loads session.js from the manifest's background scripts; a Chrome
// service worker takes one file, so it imports it here.
if (typeof importScripts === 'function') importScripts('session.js', 'changes.js');

function injectContentScript(tabId, callback) {
  chrome.scripting.executeScript({ target: { tabId }, files: ['changes.js', 'content.js'] }, callback);
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

const TAB_LOAD_TIMEOUT_MS = 30000;

// Opens claude.ai in a tab that doesn't take focus: a focused tab would close
// the popup, and with it the progress Fetch shows.
function openClaudeTab(callback) {
  chrome.tabs.create({ url: 'https://claude.ai/', active: false }, (tab) => {
    if (chrome.runtime.lastError || !tab) {
      callback(null, "Couldn't open a claude.ai tab. Open https://claude.ai yourself, then click Fetch again.");
      return;
    }
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      callback(null, "claude.ai didn't finish loading in 30s. Check the claude.ai tab Chronicle opened, then click Fetch again.");
    }, TAB_LOAD_TIMEOUT_MS);
    function onUpdated(tabId, info) {
      if (tabId !== tab.id || info.status !== 'complete') return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      callback(tab);
    }
    chrome.tabs.onUpdated.addListener(onUpdated);
  });
}

// The active tab if it's claude.ai, else any claude.ai tab, else a new one.
function findClaudeTab(callback) {
  chrome.tabs.query({ active: true, currentWindow: true }, (active) => {
    const tab = active && active[0];
    if (tab && tab.url && tab.url.startsWith('https://claude.ai/')) return callback(tab);
    chrome.tabs.query({ url: 'https://claude.ai/*' }, (claudeTabs) => {
      if (claudeTabs && claudeTabs.length > 0) return callback(claudeTabs[0]);
      openClaudeTab(callback);
    });
  });
}

// Popup → background: "make sure the content script is injected in a claude.ai tab"
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action !== 'ensureContentScript') return;
  findClaudeTab((tab, error) => {
    if (!tab) {
      sendResponse({ success: false, error });
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
