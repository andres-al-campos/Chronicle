#!/bin/bash
# Feature check: the popup reaches a claude.ai tab in Chrome and Firefox
# (dev/features/export.md). Builds, then drives Playwright's Chromium and the
# installed Firefox against claude.ai's login page. Needs the network; no
# sign-in. The full export needs a signed-in account, so it stays by hand.
set -uo pipefail
cd "$(dirname "$0")/../.."

./build.sh >/dev/null || { echo "✗ export: ./build.sh failed. Run it on its own to see why."; exit 1; }
version=$(node -p "require('./extension/manifest.json').version")
failed=0

uv run -q --with playwright==1.58.0 python -I tests/drive/export/chromium.py artifacts/chrome "$version" || failed=1

FIREFOX=/Applications/Firefox.app
if [ ! -d "$FIREFOX" ]; then
    echo "✗ export (Firefox): no $FIREFOX. Install Firefox, then rerun."
    exit 1
fi
# A fixed extension UUID so the check knows the popup's moz-extension:// URL.
uuid=0c7f3b8e-1d2a-4b5c-9e6f-0123456789ab
profile=$(mktemp -d)
cat > "$profile/user.js" <<EOF
user_pref("extensions.webextensions.uuids", "{\"chronicle-export@local\":\"$uuid\"}");
user_pref("marionette.port", 2828);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
EOF
# Started through `open` so Firefox's own process sandbox works even when this
# shell is sandboxed; started from here directly, its content processes die.
open -na "$FIREFOX" --args -headless -marionette -remote-allow-system-access -no-remote -profile "$profile"
for _ in $(seq 30); do nc -z 127.0.0.1 2828 2>/dev/null && break; sleep 0.5; done
ffpid=$(pgrep -f -- "$profile" | head -1)

uv run -q --with selenium python -I tests/drive/export/firefox.py \
    "artifacts/chronicle_export-$version.zip" "$version" "$uuid" || failed=1

[ -n "$ffpid" ] && kill "$ffpid" 2>/dev/null
rm -rf "$profile"
exit $failed
