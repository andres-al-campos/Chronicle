"""Export check, Firefox half: with the built zip installed as a temporary
add-on, check claude.ai access is granted at install, the popup reaches a
claude.ai tab the way Fetch does, the sign-in gate hides the controls (this
profile is signed out, so claude.ai answers signed out or with a Cloudflare
challenge), and with access revoked the gate says where to turn it back on.

Firefox is started by export.sh (through `open`, so its own process sandbox
works inside a sandboxed shell) with Marionette on port 2828; this attaches.

Usage: firefox.py <zip> <expected version> <extension uuid>
"""
import sys
import time

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

zip_path, version, uuid = sys.argv[1], sys.argv[2], sys.argv[3]

ENSURE_AND_PING = """
    // A background that never answers would hang Fetch; fail instead.
    setTimeout(() => done({ error: 'no answer from the background in 10s' }), 10000);
    chrome.runtime.sendMessage({ action: 'ensureContentScript' }, (res) => {
        if (chrome.runtime.lastError) return done({ error: chrome.runtime.lastError.message });
        if (!res || !res.success) return done({ error: res && res.error });
        chrome.tabs.sendMessage(res.tabId, { action: 'ping' }, (pong) => {
            done({ pong: pong && JSON.parse(JSON.stringify(pong)),
                   error: chrome.runtime.lastError && chrome.runtime.lastError.message });
        });
    });"""
GATE = """return {
    gated: document.body.classList.contains('gated'),
    title: document.getElementById('gateTitle').textContent,
    detail: document.getElementById('gateDetail').textContent,
    button: document.getElementById('gateBtn').textContent,
    controls: getComputedStyle(document.getElementById('setupPanel')).display !== 'none',
};"""
SIGNED_OUT = "You're signed out of claude.ai"
CHALLENGE = "Open claude.ai first"
HAS_CLAUDE = "chrome.permissions.contains({ origins: ['https://claude.ai/*'] }).then(done);"


def fail(what):
    print(f"✗ export (Firefox): {what}")
    sys.exit(1)


try:
    d = webdriver.Firefox(options=Options(), service=Service(
        service_args=["--connect-existing", "--marionette-port", "2828"]))
except WebDriverException as e:
    fail(f"couldn't attach to Firefox on Marionette port 2828 ({e.msg}). "
         "Check nothing else holds port 2828, then rerun ./build.sh drive export.")


def run(js):
    return d.execute_async_script("const done = arguments[arguments.length - 1];" + js)


def open_popup():
    # Marionette won't drive a content tab to moz-extension://; open it from chrome.
    d.set_context("chrome")
    d.execute_script(
        "gBrowser.selectedTab = gBrowser.addTab(arguments[0], "
        "{ triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal() });",
        f"moz-extension://{uuid}/popup.html")
    d.set_context("content")
    time.sleep(1)
    d.switch_to.window(d.window_handles[-1])


def settled_gate():
    for _ in range(100):
        g = d.execute_script(GATE)
        if not g["gated"] or g["title"] != "Checking claude.ai…":
            return g
        time.sleep(0.1)
    fail("the popup still says 'Checking claude.ai…' after 10s. Look at checkClaudeSession in session.js.")


try:
    d.install_addon(zip_path, temporary=True)
    d.get("https://claude.ai/login")
    time.sleep(2)

    open_popup()

    label = d.find_element("id", "version").text
    if label != f"v{version}":
        fail(f"popup shows {label!r}, expected 'v{version}'. Rebuild with ./build.sh and rerun.")
    if not run(HAS_CLAUDE):
        fail("claude.ai access wasn't granted at install. Check host_permissions in manifest.json "
             "and that strict_min_version is 127 or later.")
    res = run(ENSURE_AND_PING)
    pong = res.get("pong") or {}
    if res.get("error") or pong.get("version") != version:
        fail(f"popup couldn't reach the claude.ai tab: {res}. "
             "Look at background.js (injection) and content.js (the ping handler).")

    # With no claude.ai tab open, Fetch opens one without taking focus.
    popup = d.current_window_handle
    for handle in d.window_handles:
        d.switch_to.window(handle)
        if d.current_url.startswith("https://claude.ai/"):
            d.close()
    d.switch_to.window(popup)
    before = set(d.window_handles)
    res = run(ENSURE_AND_PING)
    pong = res.get("pong") or {}
    if res.get("error") or pong.get("version") != version or not set(d.window_handles) - before:
        fail(f"with no claude.ai tab open, Fetch didn't reach a new one: {res}. "
             "Look at openClaudeTab in background.js.")
    d.set_context("chrome")
    front = d.execute_script("return gBrowser.selectedBrowser.currentURI.spec")
    d.set_context("content")
    if not front.startswith("moz-extension://"):
        fail(f"the claude.ai tab Fetch opened took focus ({front}), which closes the popup. "
             "Open it with active: false in background.js.")

    gate = settled_gate()
    if not gate["gated"] or gate["controls"] or gate["title"] not in (SIGNED_OUT, CHALLENGE):
        fail(f"signed out, the popup should hide its controls behind the gate; it shows {gate}. "
             "Look at runGate in popup.js and checkClaudeSession in session.js.")
    if gate["title"] == SIGNED_OUT:
        for _ in range(50):
            badge = run("chrome.action.getBadgeText({}).then(done);")
            if badge == "!":
                break
            time.sleep(0.1)
        else:
            fail(f"signed out, the toolbar badge says {badge!r}, expected '!'. Look at refreshBadge in background.js.")
    shown_as = None
    # The weekly reminder, run from the background page; clicking it shows the popup.
    if gate["title"] == SIGNED_OUT:
        days = run("chrome.alarms.get('reminder').then(a => done(a && (a.scheduledTime - Date.now()) / 864e5));")
        if days is None or not 6.9 < days <= 7:
            fail(f"the weekly reminder is due in {days} days, expected 7. Look at scheduleReminder in background.js.")
        shown = run("""chrome.runtime.getBackgroundPage().then(async (bg) => {
            const shown = [];
            const create = bg.chrome.notifications.create.bind(bg.chrome.notifications);
            bg.chrome.notifications.create = (id, opts) => { shown.push(opts.message); return create(id, opts); };
            await bg.showReminder();
            bg.chrome.notifications.create = create;
            done(shown);
        });""")
        if len(shown) != 1 or "signed out" not in shown[0]:
            fail(f"signed out, the reminder said {shown}; it should say to sign in first. "
                 "Look at showReminder in background.js.")
        run("chrome.runtime.getBackgroundPage().then((bg) => bg.openPopupFromReminder()).then(() => done());")
        time.sleep(2)
        # The toolbar popup is a panel in the browser window, not a tab; the fallback is a window.
        d.set_context("chrome")
        shown_as = d.execute_script("""
            for (const w of Services.wm.getEnumerator(null)) {
                for (const b of w.document.querySelectorAll('panel browser')) {
                    if (b.closest('panel').state === 'open' && b.currentURI.spec.endsWith('/popup.html')) return 'popup';
                }
                if (w.document.documentElement.getAttribute('chromehidden') && w.gBrowser
                    && w.gBrowser.selectedBrowser.currentURI.spec.endsWith('/popup.html')) return 'window';
            }
            return null;""")
        d.set_context("content")
        if not shown_as:
            fail("clicking the reminder showed no popup. Look at openPopupFromReminder in background.js.")
        left = run("setTimeout(() => chrome.notifications.getAll().then(done), 300);")
        if left:
            fail(f"clicking the reminder left it showing: {left}. Look at openPopupFromReminder in background.js.")
    gate_seen = "signed out, badge '!'" if gate["title"] == SIGNED_OUT else "Cloudflare challenge"
    if gate["title"] == SIGNED_OUT:
        before = set(d.window_handles)
        d.find_element("id", "gateBtn").click()
        time.sleep(2)
        new = set(d.window_handles) - before
        if not new:
            fail("Sign in opened no tab. Look at openClaude in popup.js.")
        d.switch_to.window(new.pop())
        if not d.current_url.startswith("https://claude.ai/login"):
            fail(f"Sign in opened {d.current_url}, expected claude.ai/login. Look at openClaude in popup.js.")

    # Revoke access and open the popup again, as a user would.
    open_popup()
    run("chrome.permissions.remove({ origins: ['https://claude.ai/*'] }).then(done);")
    d.refresh()
    gate = settled_gate()
    if gate["controls"] or "about:addons" not in gate["detail"]:
        fail(f"with claude.ai access off, the gate shows {gate}; it should say where to allow claude.ai. "
             "Look at NO_CLAUDE_ACCESS in session.js.")
finally:
    d.quit()

if shown_as:
    reminder_seen = ("the weekly reminder says to sign in and its click opens the "
                     + ("toolbar popup" if shown_as == "popup" else "popup in a window"))
else:
    reminder_seen = "the reminder unchecked (claude.ai sent a Cloudflare check, not 'signed out')"
print(f"✓ export (Firefox): access granted at install, popup at v{version} pings the claude.ai tab "
      f"(opening one behind when none is open), {reminder_seen}, the gate hides the controls ({gate_seen}), "
      "and with access off it says where to allow claude.ai")
