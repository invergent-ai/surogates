#!/usr/bin/env bash
# The app's native programs for macOS, for Apple silicon, into <out>: surogate-vm (native/macos),
# signed ad hoc with its one entitlement, com.apple.security.virtualization (macos/entitlements/
# vm.plist), and the hardened runtime, so it runs on the Mac that built it, in development and on
# the macOS runner. A release signs it again with the Developer ID. On a Mac with Xcode:
#
#   desktop/native/build.sh [--arch arm64] [out]   # out: desktop/native/out
#
# Apple silicon alone: no other arch is built (Intel Macs are not supported).
set -euo pipefail

DESKTOP="$(cd "$(dirname "$0")/.." && pwd)"
ARCH=arm64
if [ "${1:-}" = "--arch" ]; then
  ARCH="${2:-}"
  shift 2 || true
fi
[ "$ARCH" = arm64 ] || { echo "build.sh: the native programs are built for arm64 alone, not ${ARCH:-no arch}" >&2; exit 2; }
[ "$#" -le 1 ] || { echo "usage: desktop/native/build.sh [--arch arm64] [out]" >&2; exit 2; }
OUT="${1:-$DESKTOP/native/out}"
[ "$(uname -s)" = Darwin ] || { echo "build.sh: the native programs are macOS's: run this on a Mac with Xcode" >&2; exit 1; }

PACKAGE="$DESKTOP/native/macos"
swift build -c release --arch "$ARCH" --package-path "$PACKAGE" --product surogate-vm
BIN="$(swift build -c release --arch "$ARCH" --package-path "$PACKAGE" --show-bin-path)"
mkdir -p "$OUT"
# A copy, so the build's own stays as the linker signed it, and the signature is this script's.
rm -f "$OUT/surogate-vm"
cp "$BIN/surogate-vm" "$OUT/surogate-vm"
codesign --force --sign - --options runtime --entitlements "$DESKTOP/macos/entitlements/vm.plist" "$OUT/surogate-vm"
codesign --verify --strict "$OUT/surogate-vm"
echo "build.sh: $OUT/surogate-vm, signed ad hoc with $(codesign -d --entitlements - --xml "$OUT/surogate-vm" 2>/dev/null | grep -o 'com\.apple\.security\.[a-z.-]*' | tr '\n' ' ')"
