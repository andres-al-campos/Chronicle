"""Export check, Chromium half: load the unpacked build in Playwright's Chromium
and check the popup reaches a claude.ai tab the way Fetch does (popup →
background service worker → scripting.executeScript → content script ping).
Also walks the sign-in gate through each answer claude.ai can give, with
/api/organizations mocked, since the drive never signs in.

Needs no sign-in: the content script runs on claude.ai's login page too.

Usage: chromium.py <unpacked extension dir> <expected version>
"""
import json
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


ORGS = "https://claude.ai/api/organizations"
ANSWERS = {
    "signedIn": dict(status=200, content_type="application/json", body='[{"uuid":"org"}]'),
    "signedOut": dict(status=403, content_type="application/json",
                      body='{"type":"error","error":{"type":"permission_error"}}'),
    "challenge": dict(status=403, content_type="text/html", body="<title>Just a moment...</title>"),
}
CONVS = "https://claude.ai/api/organizations/org1/chat_conversations"
CONV_LIST = [{"uuid": "a", "updated_at": "2026-10-20T00:00:00Z"},
             {"uuid": "b", "updated_at": "2026-10-25T00:00:00Z"},
             {"uuid": "c", "updated_at": "2026-10-26T00:00:00Z"}]
BADGE = """() => new Promise((r) => chrome.action.getBadgeText({}, r))"""
GATE = """() => ({
    gated: document.body.classList.contains('gated'),
    title: document.getElementById('gateTitle').textContent,
    button: document.getElementById('gateBtn').textContent,
    controls: getComputedStyle(document.getElementById('setupPanel')).display !== 'none',
    height: Math.round(document.body.getBoundingClientRect().height),
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

        def open_popup(answer):
            ctx.unroute(ORGS)
            ctx.route(ORGS, lambda route: route.fulfill(**ANSWERS[answer]))
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"chrome-extension://{ext_id}/popup.html")
            # wait_for_function evals a string, which the extension's CSP forbids; poll instead.
            for _ in range(100):
                g = page.evaluate(GATE)
                if not g["gated"] or g["title"] != "Checking claude.ai…":
                    return page, g
                page.wait_for_timeout(100)
            fail(f"answered {answer}, the popup still says 'Checking claude.ai…' after 10s. "
                 "Look at checkClaudeSession in session.js.")

        errors = []
        gates = {}
        for answer in ("signedOut", "challenge"):
            page, gates[answer] = open_popup(answer)
            g = gates[answer]
            if not g["gated"] or g["controls"]:
                fail(f"answered {answer}, the popup shows its controls: {g}. Look at runGate in popup.js.")
            if answer == "signedOut":
                if g["button"] != "Sign in":
                    fail(f"signed out, the gate's button says {g['button']!r}, expected 'Sign in'. "
                         "Look at runGate in popup.js.")
                with ctx.expect_page(timeout=5000) as opened:
                    page.click("#gateBtn")
                login = opened.value
                login.wait_for_load_state("domcontentloaded")
                if not login.url.startswith("https://claude.ai/login"):
                    fail(f"Sign in opened {login.url}, expected claude.ai/login. Look at openClaude in popup.js.")
                login.close()
            if not page.is_closed():
                page.close()
        if gates["challenge"]["title"] == gates["signedOut"]["title"]:
            fail("a Cloudflare challenge reads as signed out; signing in again wouldn't fix it. "
                 "Look at checkClaudeSession in session.js.")

        popup, open_gate = open_popup("signedIn")
        if open_gate["gated"] or not open_gate["controls"]:
            fail(f"signed in, the popup is still gated: {open_gate}. Look at runGate in popup.js.")
        # The gate holds the controls' place, so the popup doesn't resize when they appear.
        if gates["signedOut"]["height"] != open_gate["height"]:
            fail(f"the gate is {gates['signedOut']['height']}px tall and the controls {open_gate['height']}px, "
                 "so the popup resizes when they swap. Set #gate's min-height in popup.html to match.")
        label = popup.inner_text("#version")
        if label != f"v{version}":
            fail(f"popup shows {label!r}, expected 'v{version}'. Rebuild with ./build.sh and rerun.")
        res = popup.evaluate(ENSURE_AND_PING)
        pong = res.get("pong") or {}
        if res.get("error") or pong.get("version") != version:
            fail(f"popup couldn't reach the claude.ai tab: {res}. "
                 "Look at background.js (injection) and content.js (the ping handler).")

        # With no claude.ai tab open, Fetch opens one without taking focus.
        claude.close()
        res = popup.evaluate(ENSURE_AND_PING)
        pong = res.get("pong") or {}
        opened = [pg for pg in ctx.pages if pg.url.startswith("https://claude.ai/")]
        if res.get("error") or pong.get("version") != version or not opened:
            fail(f"with no claude.ai tab open, Fetch didn't reach a new one: {res}. "
                 "Look at openClaudeTab in background.js.")
        front = popup.evaluate("() => new Promise((r) => chrome.tabs.query("
                               "{ active: true, currentWindow: true }, (t) => r(t[0] && t[0].url)))")
        if not (front or "").startswith("chrome-extension://"):
            fail(f"the claude.ai tab Fetch opened took focus ({front}), which closes a real popup. "
                 "Open it with active: false in background.js.")

        # The badge counts what changed since the last export, and says "!" when signed out.
        def badge_becomes(expected, after):
            for _ in range(50):
                text = popup.evaluate(BADGE)
                if text == expected:
                    return
                popup.wait_for_timeout(100)
            fail(f"{after}, the toolbar badge says {text!r}, expected {expected!r}. "
                 "Look at refreshBadge in background.js and isUnchanged in changes.js.")

        ctx.unroute(ORGS)
        ctx.route(ORGS, lambda route: route.fulfill(
            status=200, content_type="application/json", body='[{"uuid":"org1","capabilities":["chat"]}]'))
        ctx.route(CONVS, lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(CONV_LIST)))
        popup.reload()
        badge_becomes("3", "signed in with 3 conversations and nothing exported")
        # As a download records it: a exported as is, b exported before its latest change.
        popup.evaluate("() => chrome.storage.local.set({ seenConversations: "
                       "{ a: '2026-10-20T00:00:00Z', b: '2026-10-24T00:00:00Z' } })")
        badge_becomes("2", "after exporting 1 of 3 unchanged")

        # The weekly reminder. A notification's text can't be read back, so record it.
        bg = ctx.service_workers[0]
        days = bg.evaluate("() => chrome.alarms.get('reminder').then(a => a && (a.scheduledTime - Date.now()) / 864e5)")
        if days is None or not 6.9 < days <= 7:
            fail(f"the weekly reminder is due in {days} days, expected 7. Look at scheduleReminder in background.js.")
        bg.evaluate("""() => {
            self.__shown = [];
            const create = chrome.notifications.create.bind(chrome.notifications);
            chrome.notifications.create = (id, opts) => { self.__shown.push(opts.message); return create(id, opts); };
        }""")
        bg.evaluate("() => showReminder()")
        shown = bg.evaluate("() => self.__shown")
        if len(shown) != 1 or not shown[0].startswith("2 conversations changed"):
            fail(f"with 2 conversations changed, the reminder said {shown}. Look at showReminder in background.js.")
        bg.evaluate("() => openPopupFromReminder()")
        left = bg.evaluate("() => new Promise((r) => setTimeout(() => chrome.notifications.getAll().then(r), 300))")
        if left:
            fail(f"clicking the reminder left it showing: {left}. Look at openPopupFromReminder in background.js.")
        popup.evaluate("() => chrome.storage.local.set({ seenConversations: { a: '2026-10-20T00:00:00Z', "
                       "b: '2026-10-25T00:00:00Z', c: '2026-10-26T00:00:00Z' } })")
        badge_becomes("", "after exporting everything")
        bg.evaluate("() => showReminder()")
        shown = bg.evaluate("() => self.__shown")
        if len(shown) != 1:
            fail(f"with nothing changed, the reminder still said {shown[1:]}; a week with nothing new sends none. "
                 "Look at showReminder in background.js.")
        # A browser closed when the reminder was due reminds soon after it starts.
        bg.evaluate("() => chrome.storage.local.set({ lastReminderAt: Date.now() - 8 * 864e5 }).then(scheduleReminder)")
        minutes = bg.evaluate("() => chrome.alarms.get('reminder').then(a => (a.scheduledTime - Date.now()) / 6e4)")
        if not minutes <= 1.1:
            fail(f"a week overdue, the reminder is due in {minutes:.0f} minutes, expected about 1. "
                 "Look at scheduleReminder in background.js.")
        ctx.unroute(ORGS)
        ctx.route(ORGS, lambda route: route.fulfill(**ANSWERS["signedOut"]))
        popup.reload()
        badge_becomes("!", "signed out")

        if errors:
            fail(f"popup threw: {errors}. Open popup.html's console in chrome://extensions to see where.")
    finally:
        ctx.close()

print(f"✓ export (Chromium): service worker up, popup at v{version} pings the claude.ai tab's content script; "
      "the sign-in gate hides the controls when signed out or challenged, Sign in opens claude.ai/login, "
      "the controls take the gate's place without a resize, with no claude.ai tab Fetch opens one behind, "
      "the badge counts what changed since the last export or says '!' when signed out, "
      "and the weekly reminder says how many changed, skips a week with none, and is due soon when overdue")
