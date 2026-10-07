#!/bin/sh
# Runs its arguments apart from the user's session, as the headed browser tests must
# (test/isolated.ts checks it): on a display of xvfb's own, as an X11 session, so the
# browser finds no Wayland compositor; with a dead session bus, so it never reaches the
# user's keyring; and with a scratch home and XDG folders, removed after.
set -eu
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
mkdir -m 700 "$scratch/run"
mkdir "$scratch/home" "$scratch/config" "$scratch/data" "$scratch/cache" "$scratch/state"
env -u WAYLAND_DISPLAY -u DISPLAY -u XAUTHORITY \
  HOME="$scratch/home" XDG_RUNTIME_DIR="$scratch/run" XDG_CONFIG_HOME="$scratch/config" \
  XDG_DATA_HOME="$scratch/data" XDG_CACHE_HOME="$scratch/cache" XDG_STATE_HOME="$scratch/state" \
  XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 DBUS_SESSION_BUS_ADDRESS=disabled: SUROGATE_BROWSER_TESTS=1 \
  xvfb-run -a "$@"
