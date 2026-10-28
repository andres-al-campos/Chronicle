// Chronicle Export — is the user signed in to claude.ai?
// Shared by the popup and the background. One cheap request from the
// extension itself, so no claude.ai tab has to be open.

const CLAUDE_ORIGINS = { origins: ['https://claude.ai/*'] };
const SESSION_TIMEOUT_MS = 8000;

// MV3 lets the user take claude.ai access away after install; say where to give it back.
const NO_CLAUDE_ACCESS = "Chronicle isn't allowed to read claude.ai. "
  + 'In Firefox: about:addons → Chronicle Export → Permissions, turn on claude.ai. '
  + 'In Chrome: chrome://extensions → Chronicle Export → Details → Site access, allow claude.ai. '
  + 'Then open Chronicle Export again.';

function hasClaudeAccess() {
  return new Promise((resolve) => chrome.permissions.contains(CLAUDE_ORIGINS, resolve));
}

// Resolves to { state, detail }:
//   signedIn  – the API answered with the user's organizations (in orgs).
//   signedOut – the API answered as JSON that the session is missing or invalid.
//   noAccess  – the user took claude.ai access away from the extension.
//   unknown   – anything else: offline, claude.ai down, or Cloudflare asking the
//               browser to check in. Never shown as "signed out", since signing
//               in again wouldn't fix it.
async function checkClaudeSession() {
  if (!(await hasClaudeAccess())) return { state: 'noAccess', detail: NO_CLAUDE_ACCESS };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), SESSION_TIMEOUT_MS);
  let res;
  try {
    res = await fetch('https://claude.ai/api/organizations', {
      credentials: 'include',
      signal: controller.signal
    });
  } catch (err) {
    return { state: 'unknown', detail: 'offline' };
  } finally {
    clearTimeout(timer);
  }

  const isJson = (res.headers.get('content-type') || '').includes('application/json');
  if (res.ok && isJson) {
    const orgs = await res.json().catch(() => null);
    if (Array.isArray(orgs) && orgs.length > 0) return { state: 'signedIn', orgs };
    return { state: 'unknown', detail: 'unexpected' };
  }
  if ((res.status === 401 || res.status === 403) && isJson) return { state: 'signedOut' };
  // Cloudflare's "Just a moment…" page comes back as 403 HTML.
  if (res.status === 403) return { state: 'unknown', detail: 'challenge' };
  return { state: 'unknown', detail: 'unexpected' };
}
