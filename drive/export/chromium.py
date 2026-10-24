"""Export check, Chromium half: load the unpacked build in Playwright's Chromium
and check the popup reaches a claude.ai tab the way Fetch does (popup →
background service worker → scripting.executeScript → content script ping).

Needs no sign-in: the content script runs on claude.ai's login page too.

Usage: chromium.py <unpacked extension dir> <expected version>
"""
import sys
import tempfile

from playwright.sync_api import Error, sync_playwright

ext, version = sys.argv[1], sys.argv[2]

ENSURE_AND_PING = """() => new Promise((resolve) => {
    // A background that never answers would hang Fetch; fail instead.
    setTimeout(() => resolve({ error: 'no answer from the background in 10s' }), 10000);
    chrome.runtime.sendMessage({ action: 'ensureContentScript' }, (res) => {
        if (chrome.runtime.lastError) return resolve({ error: chrome.runtime.lastError.message });
        if (!res || !res.success) return resolve({ error: res && res.error });
        chrome.tabs.sendMessage(res.tabId, { action: 'ping' }, (pong) => {
            resolve({ pong, error: chrome.runtime.lastError && chrome.runtime.lastError.message });
        });
    });
})"""


def fail(what):
    print(f"✗ export (Chromium): {what}")
    sys.exit(1)


with sync_playwright() as p:
    try:
        ctx = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(), headless=True, channel="chromium",
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"],
        )
    except Error as e:
        fail(f"Chromium didn't start ({str(e).splitlines()[0]}). "
             "Install Playwright's browser with `uvx playwright@1.58.0 install chromium`, then rerun.")
    try:
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = sw.url.split("/")[2]

        claude = ctx.new_page()
        claude.goto("https://claude.ai/login", wait_until="domcontentloaded")
        claude.wait_for_timeout(2000)

        popup = ctx.new_page()
        errors = []
        popup.on("pageerror", lambda e: errors.append(str(e)))
        popup.goto(f"chrome-extension://{ext_id}/popup.html")
        label = popup.inner_text("#version")
        if label != f"v{version}":
            fail(f"popup shows {label!r}, expected 'v{version}'. Rebuild with ./build.sh and rerun.")
        res = popup.evaluate(ENSURE_AND_PING)
        pong = res.get("pong") or {}
        if res.get("error") or pong.get("version") != version:
            fail(f"popup couldn't reach the claude.ai tab: {res}. "
                 "Look at background.js (injection) and content.js (the ping handler).")
        if errors:
            fail(f"popup threw: {errors}. Open popup.html's console in chrome://extensions to see where.")
    finally:
        ctx.close()

print(f"✓ export (Chromium): service worker up, popup at v{version} pings the claude.ai tab's content script")
