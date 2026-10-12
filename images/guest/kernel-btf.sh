#!/usr/bin/env bash
# The arm64 kernel's BTF, raw, as bpftool reads it, out of its uncompressed Image
# (kernel-image.sh). arm64's Image is the kernel's loaded sections laid out from its text on, so
# it holds the .BTF section's bytes, which the running kernel serves as /sys/kernel/btf/vmlinux,
# at __start_BTF less _text, up to __stop_BTF: System.map, which the modules package carries,
# names the three. What is there is checked to be BTF (magic 0xeB9F, version 1, a header of 24
# bytes). Nothing is left at <out> unless it is.
#
# Usage:
#   images/guest/kernel-btf.sh <Image> <System.map> <out>
set -euo pipefail

usage="usage: kernel-btf.sh <Image> <System.map> <out>"
IMAGE="${1:?$usage}"
MAP="${2:?$usage}"
OUT="${3:?$usage}"
fail() {
  rm -f "$OUT.partial"
  echo "kernel-btf.sh: $*" >&2
  exit 1
}
# The address System.map gives *symbol*, in hex, or nothing.
address() { awk -v want="$1" '$3 == want { print $1; exit }' "$MAP"; }
for symbol in _text __start_BTF __stop_BTF; do
  [ -n "$(address "$symbol")" ] || fail "$MAP names no $symbol"
done
start=$((0x$(address __start_BTF) - 0x$(address _text)))
size=$((0x$(address __stop_BTF) - 0x$(address __start_BTF)))
[ "$start" -gt 0 ] && [ "$size" -gt 24 ] || fail "$MAP puts the .BTF section outside the kernel's text"
dd if="$IMAGE" of="$OUT.partial" bs=1M iflag=skip_bytes,count_bytes skip="$start" count="$size" status=none || fail "cannot read $IMAGE"
[ "$(od -An -v -tx1 -N 8 "$OUT.partial" | tr -d ' \n')" = 9feb010018000000 ] \
  && [ "$(stat -c %s "$OUT.partial")" = "$size" ] \
  || fail "$IMAGE has no BTF where $MAP puts its .BTF"
mv "$OUT.partial" "$OUT"
