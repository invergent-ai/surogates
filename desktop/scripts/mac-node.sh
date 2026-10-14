#!/usr/bin/env bash
# The node a Mac's development build runs the app's hosts on, at bin/node: the PATH's own, for scripts/mac-build.sh
# and test/acceptance/macos/file-host.sh. Copied beside bin/node and renamed over it, never copied over it in place:
# macOS kills a signed program whose file was written over, at its next start, though its bytes and signature are the
# same as before.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p bin
cp "$(command -v node)" bin/node.new
mv -f bin/node.new bin/node
