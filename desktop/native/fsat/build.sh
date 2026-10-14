#!/usr/bin/env bash
# Builds fsat, the file host's calls relative to a folder it holds open on macOS (src/files/fsat.ts), for Apple
# silicon, at dist/native/fsat.node beside the app's code, and the program its test holds fsat's numbers against at
# native/fsat/build/constants. On a Mac, with Xcode's clang and a Node 22 whose headers are beside it:
#   desktop/native/fsat/build.sh
# NODE_INCLUDE names another folder holding node_api.h. The addon's N-API symbols are the node's that loads it.
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "fsat is built on macOS" >&2; exit 1; }
DESKTOP="$(cd "$(dirname "$0")/../.." && pwd)"
NODE_INCLUDE="${NODE_INCLUDE:-$(dirname "$(dirname "$(command -v node)")")/include/node}"
[ -f "$NODE_INCLUDE/node_api.h" ] || { echo "no node_api.h in $NODE_INCLUDE: set NODE_INCLUDE" >&2; exit 1; }
FLAGS=(-std=c11 -O2 -Wall -Wextra -Werror -arch arm64 -mmacosx-version-min=14.0)
mkdir -p "$DESKTOP/dist/native" "$DESKTOP/native/fsat/build"
xcrun clang "${FLAGS[@]}" -bundle -undefined dynamic_lookup -I"$NODE_INCLUDE" \
  -o "$DESKTOP/dist/native/fsat.node" "$DESKTOP/native/fsat/fsat.c"
xcrun clang "${FLAGS[@]}" -o "$DESKTOP/native/fsat/build/constants" "$DESKTOP/native/fsat/test.c"
