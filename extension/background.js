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

// ──────────────────────────── toolbar badge ────────────────────────────
// How many conversations changed since the last export, or "!" when Chronicle
// can't read claude.ai as you. Checked hourly and whenever an export or the
// popup might have changed the answer. Only lists conversations: no tab, and
// nothing is downloaded or summarized.

const BADGE_ALARM = 'badge';
const BADGE_PERIOD_MIN = 60;
const BADGE_COUNT_COLOR = '#5F6368';
const BADGE_WARN_COLOR = '#C5221F';

function setBadge(text, color, title) {
  chrome.action.setBadgeText({ text });
  if (color) chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

async function countChanged(orgs) {
  const url = `https://claude.ai/api/organizations/${pickChatOrg(orgs)}/chat_conversations`;
  const res = await fetch(url, { credentials: 'include', headers: { Accept: 'application/json' } });
  if (!res.ok) return null;
  const convs = await res.json().catch(() => null);
  if (!Array.isArray(convs)) return null;
  const { seenConversations = {} } = await chrome.storage.local.get(['seenConversations']);
  return convs.filter((c) => !isUnchanged(c, seenConversations)).length;
}

// { state } from checkClaudeSession, plus changed (a count, or null when the
// list couldn't be read) when signed in.
async function readExportState() {
  const session = await checkClaudeSession();
  if (session.state !== 'signedIn') return session;
  const changed = await countChanged(session.orgs).catch(() => null);
  return { state: 'signedIn', changed };
}

async function refreshBadge() {
  const { state, changed } = await readExportState();
  if (state === 'signedOut') {
    return setBadge('!', BADGE_WARN_COLOR, "Chronicle Export: you're signed out of claude.ai. Open it to sign in.");
  }
  if (state === 'noAccess') {
    return setBadge('!', BADGE_WARN_COLOR, "Chronicle Export can't read claude.ai. Open it to see how to allow it.");
  }
  // Can't tell (offline, a Cloudflare check, an unreadable list): keep what the badge last said.
  if (state !== 'signedIn' || changed === null) return;
  const text = changed === 0 ? '' : changed > 99 ? '99+' : String(changed);
  const title = changed === 0
    ? 'Chronicle Export: nothing changed since your last export.'
    : `Chronicle Export: ${changed} conversation${changed === 1 ? '' : 's'} changed since your last export.`;
  setBadge(text, BADGE_COUNT_COLOR, title);
}

chrome.alarms.get(BADGE_ALARM, (alarm) => {
  if (!alarm) chrome.alarms.create(BADGE_ALARM, { delayInMinutes: 1, periodInMinutes: BADGE_PERIOD_MIN });
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === BADGE_ALARM) refreshBadge();
});
chrome.runtime.onStartup.addListener(refreshBadge);
chrome.runtime.onInstalled.addListener(refreshBadge);
// A download records what was exported, so the count drops.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.seenConversations) refreshBadge();
});
// The popup asks after its own sign-in check, since you may have just signed in.
chrome.runtime.onMessage.addListener((request) => {
  if (request.action === 'refreshBadge') refreshBadge();
});

// ──────────────────────────── weekly reminder ────────────────────────────
// A notification once a week, so exporting doesn't depend on remembering.
// Clicking it opens the popup; nothing opens on its own. Firefox forgets alarms
// when it quits, so the due time lives in storage and the alarm is set again
// from it whenever the background starts.

const REMINDER_ALARM = 'reminder';
const REMINDER_ID = 'chronicle-reminder';
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

async function scheduleReminder() {
  let { lastReminderAt } = await chrome.storage.local.get(['lastReminderAt']);
  if (!lastReminderAt) {
    lastReminderAt = Date.now();
    await chrome.storage.local.set({ lastReminderAt });
  }
  // Overdue (the browser was closed when it was due): remind a minute after start.
  const when = Math.max(lastReminderAt + WEEK_MS, Date.now() + 60 * 1000);
  chrome.alarms.create(REMINDER_ALARM, { when });
}

async function showReminder() {
  await chrome.storage.local.set({ lastReminderAt: Date.now() });
  scheduleReminder();
  const { state, changed } = await readExportState();
  let message;
  if (state === 'signedIn' && changed === 0) return; // nothing to export this week
  if (state === 'signedIn' && changed !== null) {
    message = `${changed} conversation${changed === 1 ? '' : 's'} changed since your last export. Click to export.`;
  } else if (state === 'signedOut') {
    message = "Time to export. You're signed out of claude.ai, so click to sign in first.";
  } else {
    message = 'Time to export your Claude conversations. Click to open Chronicle Export.';
  }
  chrome.notifications.create(REMINDER_ID, {
    type: 'basic',
    iconUrl: chrome.runtime.getURL('icon128.png'),
    title: 'Chronicle Export',
    message
  });
}

// openPopup needs a user gesture in some browsers, and a notification click
// doesn't always count; a window showing the same popup is the fallback.
async function openPopupFromReminder() {
  chrome.notifications.clear(REMINDER_ID);
  try {
    await chrome.action.openPopup();
  } catch (err) {
    chrome.windows.create({ url: chrome.runtime.getURL('popup.html'), type: 'popup', width: 520, height: 640 });
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === REMINDER_ALARM) showReminder();
});
chrome.notifications.onClicked.addListener((id) => {
  if (id === REMINDER_ID) openPopupFromReminder();
});
chrome.alarms.get(REMINDER_ALARM, (alarm) => {
  if (!alarm) scheduleReminder();
});
