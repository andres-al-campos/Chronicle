#!/bin/bash
# build.sh - Build the Chronicle Export extension for Firefox and Chrome.
#
# Uses the project-local web-ext pinned in extension/package.json, so the
# build is reproducible on any machine. No global web-ext needed.
#
# One Manifest V3 source runs in both browsers, so both builds ship the same
# files. Chrome can't load a zip, so the build also refreshes an unpacked copy
# in artifacts/chrome/ for chrome://extensions → Load unpacked.

set -e
cd "$(dirname "$0")/extension"

VERSION=$(node -p "require('./manifest.json').version")
ARTIFACTS=../artifacts
FIREFOX_ZIP="$ARTIFACTS/chronicle_export-$VERSION.zip"
CHROME_ZIP="$ARTIFACTS/chronicle_export-$VERSION-chrome.zip"
CHROME_DIR="$ARTIFACTS/chrome"

# Bootstrap the pinned toolchain on a fresh clone.
if [ ! -x node_modules/.bin/web-ext ]; then
  echo "📥 Installing build tooling (web-ext)..."
  npm install
fi

echo "🔍 Linting extension..."
npm run --silent lint

echo ""
echo "📦 Building extension..."
npm run --silent build

cp "$FIREFOX_ZIP" "$CHROME_ZIP"
rm -rf "$CHROME_DIR"
unzip -q "$CHROME_ZIP" -d "$CHROME_DIR"

echo ""
echo "✅ Done. In ./artifacts/:"
echo "   Firefox: chronicle_export-$VERSION.zip"
echo "   Chrome:  chronicle_export-$VERSION-chrome.zip, unpacked in chrome/"
