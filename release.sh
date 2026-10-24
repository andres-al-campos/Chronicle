#!/bin/bash
# release.sh - Cut a GitHub release for the Chronicle Export extension.
#
# Reads the version from extension/manifest.json (single source of truth),
# refuses to run on a dirty or unpushed tree, won't clobber an existing tag,
# builds fresh, and attaches the extension zip to a GitHub release.
#
# To release a new version: bump "version" in extension/manifest.json, commit,
# push, then run ./release.sh.

set -e
cd "$(dirname "$0")"

# --- Version from the single source of truth ---
VERSION=$(grep '"version"' extension/manifest.json | head -1 | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
if [ -z "$VERSION" ]; then
  echo "Could not read version from extension/manifest.json."
  echo "Fix: ensure manifest.json has a \"version\": \"x.y.z\" field, then retry."
  exit 1
fi
TAG="v$VERSION"
ARTIFACT="artifacts/chronicle_export-${VERSION}.zip"
CHROME_ARTIFACT="artifacts/chronicle_export-${VERSION}-chrome.zip"

# --- Refuse a dirty tree ---
if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree has uncommitted changes."
  echo "Fix: commit or stash them so the release matches what's on GitHub, then retry."
  exit 1
fi

# --- Refuse an unpushed tree ---
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if ! git diff --quiet "origin/$BRANCH" HEAD 2>/dev/null; then
  echo "Local $BRANCH differs from origin/$BRANCH (unpushed commits)."
  echo "Fix: 'git push' so the release tag matches the remote, then retry."
  exit 1
fi

# --- Won't clobber an existing version ---
if git rev-parse "$TAG" >/dev/null 2>&1 || gh release view "$TAG" >/dev/null 2>&1; then
  echo "Release $TAG already exists."
  echo "Fix: bump \"version\" in extension/manifest.json, commit, and push before releasing."
  exit 1
fi

# --- Build fresh ---
echo "🏗  Building $TAG..."
./build.sh
for f in "$ARTIFACT" "$CHROME_ARTIFACT"; do
  if [ ! -f "$f" ]; then
    echo "Build did not produce $f."
    echo "Fix: run ./build.sh and confirm the artifact names match the manifest version, then retry."
    exit 1
  fi
done

# --- Publish ---
echo "🚀 Creating GitHub release $TAG..."
gh release create "$TAG" "$ARTIFACT" "$CHROME_ARTIFACT" \
  --title "Chronicle Export $VERSION" \
  --generate-notes

echo "✅ Released $TAG with $ARTIFACT and $CHROME_ARTIFACT"
