#!/bin/bash
# Law: ./build.sh leaves only this version's zips in artifacts/ and moves other
# versions' into artifacts/old_versions/, and building the same version twice
# doesn't archive a copy of itself. Builds twice in a scratch copy, so the real
# artifacts/ is untouched.
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cp "$root/build.sh" "$tmp/"
rsync -a --exclude node_modules "$root/extension" "$tmp/"
ln -s "$root/extension/node_modules" "$tmp/extension/node_modules"
git -C "$tmp" init -q
mkdir -p "$tmp/artifacts"
touch "$tmp/artifacts/chronicle_export-0.0.1.zip"
version=$(node -p "require('$root/extension/manifest.json').version")

for run in 1 2; do
    out=$("$tmp/build.sh" 2>&1) || { echo "✗ old-versions: ./build.sh failed in a scratch copy:"; echo "$out" | tail -5; exit 1; }
done

problems=()
[ -f "$tmp/artifacts/old_versions/chronicle_export-0.0.1.zip" ] \
    || problems+=("an older version's zip wasn't moved to artifacts/old_versions/. Look at the archiving loop in build.sh.")
ls "$tmp/artifacts/old_versions" | grep -q -- "-$version[.-]" \
    && problems+=("building $version twice left a copy of $version in artifacts/old_versions/. The archiving loop in build.sh should skip this version's zips.")
for zip in "chronicle_export-$version.zip" "chronicle_export-$version-chrome.zip"; do
    [ -f "$tmp/artifacts/$zip" ] || problems+=("artifacts/$zip is missing after a build. Look at build.sh.")
done

if [ ${#problems[@]} -gt 0 ]; then
    printf '✗ old-versions: %s\n' "${problems[@]}"
    exit 1
fi
echo "✓ old-versions: a build moves other versions' zips to artifacts/old_versions/ and never archives its own"
