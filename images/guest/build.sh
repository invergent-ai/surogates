#!/usr/bin/env bash
# Builds Surogate Desktop's VM guest from the guest stage of
# images/sandbox/Dockerfile: rootfs.img.zst and vmlinuz into <out>, and
# rootfs.img unpacked beside them, sparse, for the VM tests.
#
# Usage:
#   images/guest/build.sh [out]     # default: images/guest/out
#
# The disk is made inside the build, so the tree keeps its owners, modes and
# capabilities and nothing here needs root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$REPO_ROOT/images/guest/out}"

docker build --target guest-disk --output "type=local,dest=$OUT" \
  --file "$REPO_ROOT/images/sandbox/Dockerfile" "$REPO_ROOT"
zstd -q -d -f "$OUT/rootfs.img.zst" -o "$OUT/rootfs.img"

printf '%s\t%s\t%s\n' file bytes "on disk"
for file in rootfs.img.zst rootfs.img vmlinuz; do
  printf '%s\t%s\t%s\n' "$file" "$(stat -c %s "$OUT/$file")" "$(du -h "$OUT/$file" | cut -f1)"
done
