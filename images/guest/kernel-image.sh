#!/usr/bin/env bash
# The arm64 guest's kernel as the VM boots it: the uncompressed Image, from the vmlinuz Ubuntu
# ships. That is an EFI zboot file (Linux's drivers/firmware/efi/libstub/zboot-header.S: "MZ",
# "zimg" at 4, the compressed payload's offset and size at 8 and 12, the compression's name at 24),
# or for kernels without zboot a gzip of the Image, or the Image itself. What QEMU's virt machine
# and Apple's VZLinuxBootLoader take is the Image, which "must support the CPU architecture of
# your Mac": checked by its header's magic, "ARM\x64" at 56 (Documentation/arch/arm64/booting.rst).
# Apple does not say which forms its loader takes; on a Mac, a gzip of the Image did not boot
# ("Internal Virtualization error"), and the Image did (desktop/test/vm/darwin/measure/boot.ts).
# Nothing is left at <Image> unless it is one.
#
# Usage:
#   images/guest/kernel-image.sh <vmlinuz> <Image>
set -euo pipefail

IN="${1:?usage: kernel-image.sh <vmlinuz> <Image>}"
OUT="${2:?usage: kernel-image.sh <vmlinuz> <Image>}"
fail() {
  rm -f "$OUT.partial"
  echo "kernel-image.sh: $*" >&2
  exit 1
}
# *count* bytes of file $1 at *offset* $2, in hex; and a little-endian u32 there.
hex() { od -An -v -tx1 -j "$2" -N "$3" "$1" | tr -d ' \n'; }
u32() { od -An -v --endian=little -tu4 -j "$2" -N 4 "$1" | tr -d ' \n'; }

if [ "$(hex "$IN" 0 2)" = 1f8b ]; then
  gzip -dc < "$IN" > "$OUT.partial" || fail "$IN is a gzip that does not unpack"
elif [ "$(hex "$IN" 0 2)" = 4d5a ] && [ "$(hex "$IN" 4 4)" = 7a696d67 ]; then
  offset="$(u32 "$IN" 8)"
  size="$(u32 "$IN" 12)"
  # The name is NUL-terminated within its 32 bytes.
  type="$(od -An -v -c -j 24 -N 32 "$IN" | tr -d ' \n' | sed 's/\\0.*//')"
  case "$type" in
    gzip) unpack=(gzip -dc) ;;
    zstd) unpack=(zstd -dcq) ;;
    *) fail "$IN is an EFI zboot file compressed with $type, which this does not unpack" ;;
  esac
  dd if="$IN" bs=1M iflag=skip_bytes,count_bytes skip="$offset" count="$size" status=none | "${unpack[@]}" > "$OUT.partial" \
    || fail "$IN's zboot payload does not unpack"
elif [ "$(hex "$IN" 0 2)" = 4d5a ]; then
  # An EFI stub's Image starts as a PE file does: taken as it is, and checked below.
  cp "$IN" "$OUT.partial"
else
  fail "$IN is neither an EFI zboot file, nor a gzip, nor an arm64 Image"
fi
[ "$(hex "$OUT.partial" 56 4)" = 41524d64 ] || fail "$IN is not an arm64 kernel: its Image has no ARM\\x64 magic"
mv "$OUT.partial" "$OUT"
