"""Export check, Firefox half: with the built zip installed as a temporary
add-on, check claude.ai access is granted at install, the popup reaches a
claude.ai tab the way Fetch does, and with access revoked Fetch says where to
turn it back on.

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


try:
    d.install_addon(zip_path, temporary=True)
    d.get("https://claude.ai/login")
    time.sleep(2)
    claude = d.current_window_handle

    # Marionette won't drive a content tab to moz-extension://; open it from chrome.
    d.set_context("chrome")
    d.execute_script(
        "gBrowser.selectedTab = gBrowser.addTab(arguments[0], "
        "{ triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal() });",
        f"moz-extension://{uuid}/popup.html")
    d.set_context("content")
    time.sleep(1)
    d.switch_to.window(d.window_handles[-1])

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

    # Revoke access, reload claude.ai so the declared content script is gone,
    # and press Fetch as a user would.
    run("chrome.permissions.remove({ origins: ['https://claude.ai/*'] }).then(done);")
    popup = d.current_window_handle
    d.switch_to.window(claude)
    d.refresh()
    time.sleep(2)
    d.switch_to.window(popup)
    d.find_element("id", "fetchBtn").click()
    time.sleep(1)
    status = d.find_element("id", "status").text
    if "about:addons" not in status:
        fail(f"with claude.ai access off, Fetch says {status!r}; it should say where to allow claude.ai. "
             "Look at explainInjectError in background.js.")
finally:
    d.quit()

print(f"✓ export (Firefox): access granted at install, popup at v{version} pings the claude.ai tab, "
      "and with access off Fetch says where to allow it")
