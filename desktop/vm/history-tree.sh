#!/usr/bin/env bash
# A folder's history as the agent disk carries it, into <dir>: the cloud's history, its form for
# a folder of this computer, and the one module they import, under empty packages of their
# names, with the agent's way in beside them. Nothing else of the platform is in the guest.
#
# Usage:
#   desktop/vm/history-tree.sh <dir>
set -euo pipefail

VM="$(dirname "$(readlink -f "$0")")"
REPO="$(cd "$VM/../.." && pwd)"
OUT="$1"

mkdir -p "$OUT/surogates/sandbox" "$OUT/surogates/tools/utils"
cp "$VM/history.py" "$OUT/main.py"
cp "$REPO/surogates/sandbox/history.py" "$REPO/surogates/sandbox/local_history.py" "$OUT/surogates/sandbox/"
cp "$REPO/surogates/tools/utils/checkpoint_manager.py" "$OUT/surogates/tools/utils/"
for package in surogates surogates/sandbox surogates/tools surogates/tools/utils; do
  : > "$OUT/$package/__init__.py"
done
