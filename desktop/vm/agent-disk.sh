#!/usr/bin/env bash
# The agent disk: a small read-only ext4 holding the guest agent and the rest of
# the guest's init. The image's stub init mounts it at /run/surogate/agent and
# runs its init. It ships in the app's tarball, so the agent changes with every
# app release and the image only when the tools do.
#
# Usage, after npm run build:
#   desktop/vm/agent-disk.sh <agent.img>
set -euo pipefail

DESKTOP="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1"

tree="$(mktemp -d)"
trap 'rm -rf "$tree"' EXIT
# The agent and the modules it imports: guest/, files/ and the link protocol's outcomes.
mkdir -p "$tree/link"
cp -r "$DESKTOP/dist/guest" "$DESKTOP/dist/files" "$tree/"
cp "$DESKTOP/dist/link/protocol.js" "$tree/link/"
find "$tree" -name '*.map' -delete
cp "$DESKTOP/vm/init" "$DESKTOP/vm/enter-root" "$tree/"
echo '{"type":"module"}' > "$tree/package.json"
# Readable by every root's user, whatever umask built it.
chmod -R u=rwX,go=rX "$tree"

# Owned by root in the guest: mke2fs records the owners it sees, and in a user
# namespace of its own this user is root.
rm -f "$OUT"
unshare -r mke2fs -q -t ext4 -O ^has_journal -L surogate-agent -d "$tree" "$OUT" 16M
