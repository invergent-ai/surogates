#!/usr/bin/env bash
# The app's own node (spec, Section 11): Node 22 LTS, the floor of this package's engines, its archive checked against
# the sha256 that nodejs.org's signed SHASUMS256.txt gives, without npm. A bin/node of that version already there is
# kept, but for macOS's.
#
#   scripts/node.sh             the Linux app's, the first step of npm run build: on Linux on x64, linux-x64's,
#                               stripped, at bin/node. On Windows it refuses, naming the Windows app's build, npm run
#                               build:win32, which makes the node a development build's hosts run on, bin/node.exe, itself
#   scripts/node.sh win32-x64   the Windows app's, which its layout ships (scripts/package-win.sh): on Windows on x64,
#                               win-x64's node.exe at bin/node.exe, as nodejs.org made it and signed it, never stripped,
#                               which would break the OpenJS Foundation's signature that the app keeps
#   scripts/node.sh darwin-arm64  the macOS app's, which its bundle ships (scripts/package-mac.sh): on a Mac with Apple
#                               silicon, darwin-arm64's at bin/node, never stripped, renamed over the one there and never
#                               written over it: macOS kills a signed program whose file was written over in place, at its
#                               next start, and fetched whatever node is there. The release signs it again, with the
#                               app (scripts/sign-mac.sh)
#
# Any other computer is refused.
#
# The pin moves in one change: VERSION and each SHA256 together, each hash read from that release's
# SHASUMS256.txt once `gpg --verify SHASUMS256.txt.asc` passes with nodejs/release-keys' keys,
# each fetched with `curl -q` and checked in a keyring of its own, never the user's. native/ntat/build.ps1 pins the
# headers and win-x64/node.lib of the same version, which ntat is built against.
set -euo pipefail

VERSION=v22.23.3
case "${1:-}-$(uname -s)-$(uname -m)" in
  -Linux-x86_64) TARGET=linux-x64 SHA256=1084aa36196bba4c3a5e69a1ee388a6e4ff729dad09445fbcd434b28fe3c24af ;;
  win32-x64-MINGW64_NT*-x86_64 | win32-x64-MSYS_NT*-x86_64) TARGET=win-x64 SHA256=2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71 ;;
  darwin-arm64-Darwin-arm64) TARGET=darwin-arm64 SHA256=23b25245dcfb9af7262f8ff142e9e2e0af025368117329e7a7458a51e5922f53 ;;
  -MINGW* | -MSYS* | -CYGWIN*)
    echo "npm run build is the Linux app's build, with Linux's node: on Windows, build the app with npm run build:win32" >&2
    exit 1
    ;;
  *) echo "node.sh fetches the Linux app's node on Linux on x64, with darwin-arm64 the macOS app's on a Mac with Apple silicon, and with win32-x64 the Windows app's on Windows on x64" >&2; exit 1 ;;
esac

cd "$(dirname "$0")/.."
if [ "$TARGET" = win-x64 ]; then
  [ "$(bin/node.exe --version 2>/dev/null)" = "$VERSION" ] && exit 0
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  # -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it.
  curl -q -fsSL --proto '=https' -o "$tmp/node.zip" "https://nodejs.org/dist/$VERSION/node-$VERSION-win-x64.zip"
  echo "$SHA256  $tmp/node.zip" | sha256sum --check --quiet
  unzip -q "$tmp/node.zip" "node-$VERSION-win-x64/node.exe" -d "$tmp"
  mkdir -p bin
  mv "$tmp/node-$VERSION-win-x64/node.exe" bin/node.exe
  exit 0
fi
if [ "$TARGET" = darwin-arm64 ]; then
  # Fetched whatever bin/node says it is: a Mac's build puts the PATH's node there (scripts/mac-node.sh), and the release
  # ships the pinned bytes alone, checked by their hash.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  # -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it.
  curl -q -fsSL --proto '=https' -o "$tmp/node.tar.gz" "https://nodejs.org/dist/$VERSION/node-$VERSION-darwin-arm64.tar.gz"
  echo "$SHA256  $tmp/node.tar.gz" | shasum -a 256 --check --status || { echo "node.sh: the archive is not the pinned one" >&2; exit 1; }
  tar -xzf "$tmp/node.tar.gz" -C "$tmp" --strip-components=2 "node-$VERSION-darwin-arm64/bin/node"
  mkdir -p bin
  mv "$tmp/node" bin/node.new
  mv -f bin/node.new bin/node
  exit 0
fi
[ "$(bin/node --version 2>/dev/null)" = "$VERSION" ] && exit 0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it.
curl -q -fsSL --proto '=https' -o "$tmp/node.tar.gz" "https://nodejs.org/dist/$VERSION/node-$VERSION-linux-x64.tar.gz"
echo "$SHA256  $tmp/node.tar.gz" | sha256sum --check --quiet
tar -xzf "$tmp/node.tar.gz" -C "$tmp" --strip-components=2 "node-$VERSION-linux-x64/bin/node"
strip "$tmp/node"
mkdir -p bin
mv "$tmp/node" bin/node
