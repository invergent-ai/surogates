#!/usr/bin/env bash
# surogate-helper for this Mac (Sources/surogate-helper/main.swift), for a development build and the macOS
# runner: built by Swift Package Manager for this Mac's arm64, release, signed ad hoc, at
# .build/surogate-helper, where a development build runs it (src/mac-helper.ts). A release signs its
# own build again, with the Developer ID.
set -euo pipefail
cd "$(dirname "$0")"
swift build -c release
bin=$(swift build -c release --show-bin-path)
install -m 0755 "$bin/surogate-helper" .build/surogate-helper
codesign --force --sign - .build/surogate-helper
