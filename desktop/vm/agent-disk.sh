#!/usr/bin/env bash
# The agent disk: a small read-only ext4 holding the guest agent and the rest of
# the guest's init. The image's stub init mounts it at /run/surogate/agent and
# runs its init. It ships in the app's tarball, so the agent changes with every
# app release and the image only when the tools do.
#
# Usage, after npm run build:
#   desktop/vm/agent-disk.sh <agent.img>
# It needs mke2fs and debugfs (e2fsprogs) and fakeroot. With the same e2fsprogs,
# the same build gives the same disk, byte for byte, at the same
# SOURCE_DATE_EPOCH, which is every time in it: a release packed again is then
# the same tarball. Without one, its times are the moment it is made.
set -euo pipefail

DESKTOP="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1"
command -v fakeroot >/dev/null \
  || { echo "agent-disk.sh: fakeroot is missing, which makes the disk's files root's with no root and no user namespace: install it (apt install fakeroot)" >&2; exit 1; }

STAMP="${SOURCE_DATE_EPOCH:-$(date +%s)}"
# The disk's UUID and the seed of its folders' hashes, which mke2fs would draw at
# random. Nothing reads either: the guest mounts the disk by its device.
ID=8ed87b42-47ea-446b-b058-410e6845250c

tree="$(mktemp -d)"
trap 'rm -rf "$tree"' EXIT
# The agent and the modules it imports: guest/, files/ and the link protocol's outcomes.
mkdir -p "$tree/link"
cp -r "$DESKTOP/dist/guest" "$DESKTOP/dist/files" "$tree/"
cp "$DESKTOP/dist/link/protocol.js" "$tree/link/"
find "$tree" -name '*.map' -delete
cp "$DESKTOP/vm/init" "$DESKTOP/vm/enter-root" "$tree/"
echo '{"type":"module"}' > "$tree/package.json"
# Readable by every root's user, whatever umask built it; with no bit a folder
# takes from the one it is made in; and each with the build's time, not the
# moment it was copied here.
chmod -R u=rwX,go=rX,a-st "$tree"
find "$tree" -exec touch -h -d "@$STAMP" {} +

# Owned by root in the guest: mke2fs records the owners it sees, and under
# fakeroot it sees every file as root's. No user namespace is made for it: a
# stock Ubuntu 24.04 refuses one to a program without a profile of its own, and
# so does a container.
rm -f "$OUT"
E2FSPROGS_FAKE_TIME="$STAMP" fakeroot mke2fs -q -t ext4 -O ^has_journal -L surogate-agent -U "$ID" -E hash_seed="$ID" -d "$tree" "$OUT" 16M
# mke2fs records each file's time of change as it finds it: the moment of the
# touch above, which nothing can set on this side. debugfs sets it in the disk.
# It ends 0 whatever it could not do, and says that after its name and version.
said="$(find "$tree" -mindepth 1 -printf 'sif "/%P" ctime @'"$STAMP"'\n' | debugfs -w -f - "$OUT" 2>&1 >/dev/null | grep -v '^debugfs [0-9]' || true)"
[ -z "$said" ] || { echo "agent-disk.sh: debugfs could not set a file's time in $OUT: $said" >&2; exit 1; }
