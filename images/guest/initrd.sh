#!/usr/bin/env bash
# The guest's initrd (images/guest/initrd): the cpio archive, newc, that the kernel unpacks as its first
# root where the VM boots it with one, uncompressed, as Hyper-V's LinuxKernelDirect takes it (InitRdPath).
# It holds <init> as /init, <busybox> as /bin/busybox, each module of <list> out of <modules>, a folder of
# the kernel's module files (modules.sh copy), unpacked as /lib/modules/<name>.ko, and /modules, which names
# them in <list>'s order, as /init loads them. The same inputs make the same bytes: every file root's, at
# the epoch. Nothing is left at <out> unless it is whole.
#
# Usage:
#   images/guest/initrd.sh <init> <busybox> <modules> <list> <out>
set -euo pipefail

usage="usage: images/guest/initrd.sh <init> <busybox> <modules> <list> <out>"
[ "$#" -eq 5 ] || { echo "$usage" >&2; exit 2; }
INIT="$1"
BUSYBOX="$2"
MODULES="$3"
LIST="$4"
OUT="$5"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE" "$OUT.partial"' EXIT
fail() {
  echo "initrd.sh: $*" >&2
  exit 1
}

mkdir -p "$STAGE/bin" "$STAGE/dev" "$STAGE/lib/modules" "$STAGE/proc" "$STAGE/root" "$STAGE/sys"
install -m 0755 "$INIT" "$STAGE/init"
install -m 0755 "$BUSYBOX" "$STAGE/bin/busybox"
# The list's names, a name a line, without its comments and blank lines.
names="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' "$LIST")"
[ -n "$names" ] || fail "$LIST names no module"
for name in $names; do
  if [ -f "$MODULES/$name.ko.zst" ]; then
    zstd -q -dc "$MODULES/$name.ko.zst" > "$STAGE/lib/modules/$name.ko" || fail "$MODULES/$name.ko.zst does not unpack"
  elif [ -f "$MODULES/$name.ko" ]; then
    cp "$MODULES/$name.ko" "$STAGE/lib/modules/$name.ko"
  else
    fail "$MODULES has no module $name"
  fi
  chmod 0644 "$STAGE/lib/modules/$name.ko"
done
printf '%s\n' $names > "$STAGE/modules"
chmod 0644 "$STAGE/modules"

find "$STAGE" -exec touch -h -d @0 {} +
(cd "$STAGE" && find . -mindepth 1 | LC_ALL=C sort | cpio -o -H newc -R 0:0 --reproducible --quiet) > "$OUT.partial"
mv "$OUT.partial" "$OUT"
