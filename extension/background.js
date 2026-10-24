// Chronicle Export — background script.
// Thin relay. Re-injects the content script into open claude.ai tabs on
// install/update so the popup can talk to it immediately after a reload.
// Holds no state, so it runs as a Chrome service worker or a Firefox event page.

function injectContentScript(tabId, callback) {
  chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] }, callback);
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
      sendResponse({ success: false, error: 'No active tab.' });
      return;
    }
    if (!tab.url || !tab.url.includes('claude.ai')) {
      // Fall back to any claude.ai tab if the active one isn't claude.
      chrome.tabs.query({ url: 'https://claude.ai/*' }, (claudeTabs) => {
        if (!claudeTabs || claudeTabs.length === 0) {
          sendResponse({ success: false, error: 'No claude.ai tab open. Open https://claude.ai and log in, then click Export again.' });
          return;
        }
        const target = claudeTabs[0];
        injectContentScript(target.id, () => {
          if (chrome.runtime.lastError) {
            sendResponse({ success: false, error: chrome.runtime.lastError.message });
          } else {
            sendResponse({ success: true, tabId: target.id });
          }
        });
      });
      return;
    }
    injectContentScript(tab.id, () => {
      if (chrome.runtime.lastError) {
        sendResponse({ success: false, error: chrome.runtime.lastError.message });
      } else {
        sendResponse({ success: true, tabId: tab.id });
      }
    });
  });
  return true;
});
