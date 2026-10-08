#!/usr/bin/env bash
# The release's tarball (spec, Section 9): Electron's own Linux build with the app beside it,
# laid out as electron-builder's dir target lays one out, then tarred.
#
#   surogate-desktop-<version>-linux-x64/
#     surogate                      Electron, renamed, with the app's fuses (no inspector)
#     resources/app/                the app as a folder, never an asar: dist/ (no testing/),
#                                   assets/, package.json at <version>, the production
#                                   node_modules and bin/node
#     resources/vm/                 agent.img and the guest image's manifest
#     resources/surogate.svg        the desktop entry's icon
#     bin/surogate-apply-update     the install script, which applies a verified release as root
#
# The install script adds bin/bwrap, the system's copy, to each version it installs.
#
# Usage, from desktop/, after npm run build (which fetches and checks bin/node):
#   scripts/package.sh <version> <vm manifest.json> <out>
# It writes <out>/surogate-desktop-<version>-linux-x64.tar.gz.
set -euo pipefail

VERSION="${1:-}"
VM_MANIFEST="${2:-}"
OUT="${3:-}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [ -f "$VM_MANIFEST" ] && [ -n "$OUT" ] \
  || { echo "usage: scripts/package.sh <x.y.z> <vm manifest.json> <out>" >&2; exit 2; }
cd "$(dirname "$0")/.."
DESKTOP="$PWD"

name="surogate-desktop-$VERSION-linux-x64"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
top="$stage/$name"
app="$top/resources/app"

cp -a node_modules/electron/dist "$top"
mv "$top/electron" "$top/surogate"
# Electron's own default app: a packaged Electron loads resources/app instead.
rm "$top/resources/default_app.asar"
node scripts/fuses.mjs "$top/surogate"

mkdir -p "$app" "$top/resources/vm" "$top/bin"
cp -a dist assets bin "$app/"
# The echo client and the sign-in helper are the tests' own; the agent disk ships in resources/vm.
rm -rf "$app/dist/testing" "$app/dist/agent.img"
find "$app/dist" -name '*.map' -delete
jq --arg version "$VERSION" '.version = $version' package.json > "$app/package.json"
cp package-lock.json "$app/"
(cd "$app" && NPM_CONFIG_USERCONFIG=/dev/null npm ci --omit=dev --ignore-scripts --prefer-offline --no-audit --no-fund --loglevel=error)
rm "$app/package-lock.json"

vm/agent-disk.sh "$top/resources/vm/agent.img"
cp "$VM_MANIFEST" "$top/resources/vm/manifest.json"
cp ../web/public/favicon.svg "$top/resources/surogate.svg"
cp release/install.sh "$top/bin/surogate-apply-update"

# Root installs it: nothing in it is writable but by its owner, whatever umask built it.
chmod -R u+w,go-w,a+rX "$top"
mkdir -p "$OUT"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@${SOURCE_DATE_EPOCH:-$(git -C "$DESKTOP" log -1 --format=%ct)}" \
  -C "$stage" -czf "$OUT/$name.tar.gz" "$name"
echo "$OUT/$name.tar.gz"
