#!/usr/bin/env bash
# The app's own node (spec, Section 11): Node 22 LTS for linux-x64, the floor of this package's
# engines, its tarball checked against the sha256 that nodejs.org's signed SHASUMS256.txt gives,
# stripped, without npm, at bin/node. A bin/node of that version already there is kept.
#
# The pin moves in one change: VERSION and SHA256 together, the hash read from that release's
# SHASUMS256.txt once `gpg --verify SHASUMS256.txt.asc` passes with nodejs/release-keys' keys,
# each fetched with `curl -q` and checked in a keyring of its own, never the user's.
set -euo pipefail

VERSION=v22.23.3
SHA256=1084aa36196bba4c3a5e69a1ee388a6e4ff729dad09445fbcd434b28fe3c24af

cd "$(dirname "$0")/.."
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
