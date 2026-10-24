#!/bin/bash
# build.sh - Build the Chronicle Export extension for Firefox and Chrome, or check it.
#
#   ./build.sh                 Lint and build into artifacts/ (needs Node 20+)
#   ./build.sh check           Run every law in laws/, then every drive
#   ./build.sh drive [name]    Run every feature check in drive/, or the one named
#
# Uses the project-local web-ext pinned in extension/package.json, so the
# build is reproducible on any machine. No global web-ext needed.
#
# One Manifest V3 source runs in both browsers, so both builds ship the same
# files. Chrome can't load a zip, so the build also refreshes an unpacked copy
# in artifacts/chrome/ for chrome://extensions → Load unpacked.

set -e
cd "$(dirname "$0")"
# The pre-commit hook stamps the docs a commit changes (hooks/stamp-docs.mjs).
git config core.hooksPath hooks

# Every feature check in drive/, or the one named. A name with no script is an
# error, so a typo can't pass by running nothing.
drive() {
  local name="$1" failed=0 ran=0
  if [ -n "$name" ]; then
    if [ ! -f "drive/$name.sh" ]; then
      echo "error: no drive named \"$name\": there's no drive/$name.sh. Fix the name, or write that check."
      local there
      there=$(ls drive 2>/dev/null | sed -n 's/\.sh$//p' | tr '\n' ' ' || true)
      echo "       The ones there: ${there:-none yet}"
      return 1
    fi
    "drive/$name.sh"
    return $?
  fi
  for d in drive/*.sh; do
    [ -f "$d" ] || continue
    ran=1
    "$d" || failed=1
  done
  [ "$ran" = 1 ] || echo "No drives yet: drive/ has no checks. features/README.md lists what isn't covered."
  return $failed
}

case "${1:-}" in
  "") ;;
  check)
    failed=0
    for law in laws/*.sh; do "$law" || failed=1; done
    drive "" || failed=1
    [ "$failed" = 0 ] || { echo "❌ A check failed. Each message above says what broke and how to fix it."; exit 1; }
    echo "✅ All laws and drives pass."
    exit 0
    ;;
  drive)
    drive "${2:-}"
    exit $?
    ;;
  *)
    echo "error: unknown option '$1'. Use ./build.sh, ./build.sh check, or ./build.sh drive [name]."
    exit 1
    ;;
esac

cd extension
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
