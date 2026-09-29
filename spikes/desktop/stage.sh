#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/surogate/versions/spike"
# Electron 44 downloads its binary only when install.js runs; npm ci does not run it.
if [ ! -d "$HERE/node_modules/electron/dist" ]; then
  command -v node >/dev/null || . "$HOME/.nvm/nvm.sh"
  node "$HERE/node_modules/electron/install.js"
fi
rm -rf "$DEST"
mkdir -p "$DEST/resources/app"
cp -a "$HERE/node_modules/electron/dist/." "$DEST/"
mv "$DEST/electron" "$DEST/surogate"
rsync -a --exclude node_modules/electron --exclude .git "$HERE/" "$DEST/resources/app/"
# The app's own bwrap: Ubuntu 26.04's bwrap-userns-restrict profile attaches to
# /usr/bin/bwrap only, so this copy inherits the app's AppArmor profile instead.
mkdir -p "$DEST/bin"
cp "$(command -v bwrap)" "$DEST/bin/bwrap"
echo "$DEST/surogate"
