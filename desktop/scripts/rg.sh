#!/usr/bin/env bash
# The app's own rg on macOS (src/hosts/srt-darwin.ts): ripgrep's own aarch64-apple-darwin release, its tarball checked
# against the sha256 pinned here, at bin/rg beside the app's node. It links nothing but the system's libSystem and
# libiconv, which the file helper's sandbox reads. A bin/rg of that version already there is kept. Linux's file helper
# runs the system's rg, which the install script installs.
#
# The pin moves in one change: VERSION and SHA256 together, the hash read from the release's own .sha256 file and
# checked against the tarball fetched beside it.
set -euo pipefail

VERSION=15.2.0
SHA256=3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4
NAME="ripgrep-$VERSION-aarch64-apple-darwin"

[ "$(uname -s)-$(uname -m)" = Darwin-arm64 ] || { echo "rg.sh fetches the app's rg for macOS on Apple silicon" >&2; exit 1; }
cd "$(dirname "$0")/.."
[ "$(bin/rg --version 2>/dev/null | head -n 1)" = "ripgrep $VERSION" ] && exit 0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it.
curl -q -fsSL --proto '=https' -o "$tmp/rg.tar.gz" "https://github.com/BurntSushi/ripgrep/releases/download/$VERSION/$NAME.tar.gz"
echo "$SHA256  $tmp/rg.tar.gz" | shasum -a 256 --check --status || { echo "rg.sh: the tarball is not the pinned one" >&2; exit 1; }
tar -xzf "$tmp/rg.tar.gz" -C "$tmp" "$NAME/rg"
mkdir -p bin
mv "$tmp/$NAME/rg" bin/rg
