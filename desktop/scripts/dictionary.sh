#!/usr/bin/env bash
# The app's own spelling dictionary (src/shell/spellcheck.ts): Chromium's en-US Hunspell dictionary
# from its hunspell_dictionaries, as Chromium's dictionary server serves it to every Chromium that
# asks (the very file Electron would download at a first run), checked against its sha256, at
# dictionaries/en-US-10-1.bdic. A dictionary of that hash already there is kept.
#
# The pin moves in one change with Electron's: NAME, URL and SHA256 together, the name the one a
# session of the new Electron asks for (test/spellcheck.test.ts says which).
set -euo pipefail

NAME=en-US-10-1.bdic
URL=https://redirector.gvt1.com/edgedl/chrome/dict/en-us-10-1.bdic
SHA256=a075b01d9b015c616511a9e87da77da3d9881621db32f584e4606ddabf1c1100

cd "$(dirname "$0")/.."
[ -f "dictionaries/$NAME" ] && echo "$SHA256  dictionaries/$NAME" | sha256sum --check --quiet --status && exit 0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# -q first, or curl reads the user's curlrc, whose headers, logins and netrc would go out with it. The
# server answers with a redirect to a host of its own, followed over https alone.
curl -q -fsSL --proto '=https' --proto-redir '=https' -o "$tmp/$NAME" "$URL"
echo "$SHA256  $tmp/$NAME" | sha256sum --check --quiet
mkdir -p dictionaries
mv "$tmp/$NAME" "dictionaries/$NAME"
