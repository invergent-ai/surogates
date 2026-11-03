#!/usr/bin/env bash
# The shared design tokens the shell's pages import (src/shell/pages/shell.css), from the UI kit
# (@invergent/ui-sdk, invergent-ai/ui-kit) as npm serves it, checked against its sha256, at
# build/ui-tokens/tokens.css. Only the one file is taken: the kit is not installed, so none of its
# dependencies or their install scripts reach the desktop's build.
#
# The pin moves in one change: VERSION and SHA256 together, the tarball's sha256 from
# `curl -sL "$URL" | sha256sum`.
set -euo pipefail

VERSION=2.37.1
SHA256=af02c83e143a0e18668a4c7e8f73d24fa3331196b38d060fe8d46c1cf64487ec
URL="https://registry.npmjs.org/@invergent/ui-sdk/-/ui-sdk-${VERSION}.tgz"
OUT=build/ui-tokens

# A sum checked by sha256sum on Linux, and on a Mac by shasum, as a Mac's sha256sum is BSD's, which
# checks no list (scripts/dictionary.sh checks its dictionary the same way).
if [ "$(uname -s)" = Darwin ]; then
  sha256() { shasum -a 256 --check --quiet "$@"; }
else
  sha256() { sha256sum --check --quiet "$@"; }
fi

cd "$(dirname "$0")/.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -sfL "$URL" -o "$work/ui-sdk.tgz"
echo "$SHA256  $work/ui-sdk.tgz" | sha256
mkdir -p "$OUT"
tar -xzf "$work/ui-sdk.tgz" -C "$work" package/dist/tokens.css
cp "$work/package/dist/tokens.css" "$OUT/tokens.css"
