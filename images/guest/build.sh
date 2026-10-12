#!/usr/bin/env bash
# Builds Surogate Desktop's VM guest for an arch from the guest stage of
# images/sandbox/Dockerfile into <out>: rootfs.img.zst and vmlinuz.zst, the files
# the release publishes; rootfs.img and vmlinuz unpacked beside them, sparse, for
# the VM tests, unless --packed; and manifest.json, which the app's tarball carries:
# the image's key (images/guest/inputs.sh), its arch and, for each file, its size and
# sha256 unpacked and as downloaded.
#
# Usage:
#   images/guest/build.sh [--arch amd64|arm64] [--packed] [out]   # amd64 unless it says arm64; out: images/guest/out
#
# The disk is made inside the build, so the tree keeps its owners, modes and
# capabilities and nothing here needs root. --packed, as the release builds it, leaves
# the 2.9 GB disk unpacked nowhere: its size and hash are read from zstd's stream. Docker
# runs the build's programs as the arch's own: on a computer of another arch only with an
# emulator registered for it (binfmt_misc), so the release builds each arch on its own runner.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ARCH=amd64
PACKED=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --arch) ARCH="${2:-}"; shift; shift || true ;;
    --packed) PACKED=1; shift ;;
    *) break ;;
  esac
done
case "$ARCH" in
  amd64 | arm64) [ "$#" -le 1 ] ;;
  *) false ;;
esac || { echo "usage: images/guest/build.sh [--arch amd64|arm64] [--packed] [out]" >&2; exit 2; }
OUT="${1:-$REPO_ROOT/images/guest/out}"

docker build --platform "linux/$ARCH" --target guest-disk --output "type=local,dest=$OUT" \
  --file "$REPO_ROOT/images/sandbox/Dockerfile" "$REPO_ROOT"

entries=()
for file in rootfs.img vmlinuz; do
  [ -n "$PACKED" ] || zstd -q -d -f --sparse "$OUT/$file.zst" -o "$OUT/$file"
  entries+=("$(printf '{"name":"%s","size":%s,"sha256":"%s","download":"%s.zst","downloadSize":%s,"downloadSha256":"%s"}' \
    "$file" "$(zstd -q -dc "$OUT/$file.zst" | wc -c)" "$(zstd -q -dc "$OUT/$file.zst" | sha256sum | cut -d' ' -f1)" \
    "$file" "$(stat -c %s "$OUT/$file.zst")" "$(sha256sum "$OUT/$file.zst" | cut -d' ' -f1)")")
done
printf '{"key":"%s","arch":"%s","files":[%s]}\n' "$("$REPO_ROOT/images/guest/inputs.sh" "$ARCH")" "$ARCH" "$(IFS=,; echo "${entries[*]}")" > "$OUT/manifest.json"

printf '%s\t%s\t%s\n' file bytes "on disk"
for file in rootfs.img.zst rootfs.img vmlinuz.zst vmlinuz; do
  if [ -e "$OUT/$file" ]; then printf '%s\t%s\t%s\n' "$file" "$(stat -c %s "$OUT/$file")" "$(du -h "$OUT/$file" | cut -f1)"; fi
done
