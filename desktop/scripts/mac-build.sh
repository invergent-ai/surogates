#!/usr/bin/env bash
# A development build of the whole app on a Mac (Apple silicon): the app's code and pages, and every native
# program where the app looks for it. surogate-helper at native/helper/.build and surogate-spawn at bin/, with the
# app's code (npm run build:darwin); surogate-vm (native/build.sh) at native/out; the app's rg at bin/, beside a
# node at bin/node; fsat at dist/native/fsat.node.
# On a Mac with Xcode, Node 22.14 first on the PATH, from desktop/, after `npm ci`:
#   scripts/mac-build.sh
# The guest image and the agent's disk come from test/vm/darwin/measure/prepare.sh, which needs a container
# runtime, or the arm64 guest image downloaded from its workflow's run.
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "mac-build.sh: run this on a Mac" >&2; exit 1; }
cd "$(dirname "$0")/.."
npm run electron:install
npm run build:darwin
native/build.sh
native/fsat/build.sh
mkdir -p bin
cp "$(command -v node)" bin/node
scripts/rg.sh
echo "mac-build.sh: the app, surogate-vm, surogate-spawn, fsat, rg and surogate-helper are built"
