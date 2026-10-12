#!/usr/bin/env bash
# Builds surogate-spawn, the app's way of starting a program cleanly on macOS (src/clean-child.ts),
# for Apple silicon, at bin/surogate-spawn beside the app's own node. On a Mac, with Xcode's clang:
#   desktop/native/spawn/build.sh
# The linker signs an arm64 program ad hoc, which is all a development build needs to run it.
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "surogate-spawn is built on macOS" >&2; exit 1; }
DESKTOP="$(cd "$(dirname "$0")/../.." && pwd)"
mkdir -p "$DESKTOP/bin"
xcrun clang -std=c11 -O2 -Wall -Wextra -Werror -arch arm64 -mmacosx-version-min=14.0 \
  -o "$DESKTOP/bin/surogate-spawn" "$DESKTOP/native/spawn/surogate-spawn.c"
