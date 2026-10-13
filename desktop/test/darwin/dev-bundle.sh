#!/usr/bin/env bash
# A development build of the app as a Mac app of its own, for the checks a Mac's person makes by hand:
# ~/Applications/Surogate Dev.app, this package's Electron with the surogate:// scheme in its Info.plist
# (CFBundleURLTypes), which Launch Services reads, and a bundle identifier of its own, so that its login
# item, its Keychain item and its privacy grants are never the released app's. Its code is this
# checkout's dist/, as `npm run build:darwin` built it; its executable keeps Electron's name, so the app
# runs as a development build (app.isPackaged is false). Signed ad hoc, as development builds are.
#
#   test/darwin/dev-bundle.sh            make it, register it with Launch Services, and open it
#   test/darwin/dev-bundle.sh --remove   quit it, take out its login item, take it out of Launch Services, and remove it
set -euo pipefail

cd "$(dirname "$0")/../.."
desktop=$(pwd -P)
app="$HOME/Applications/Surogate Dev.app"
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
program="$app/Contents/MacOS/Electron"
# Started with this set, the bundle takes out its own login item and quits, starting nothing of the app's: only an app
# itself unregisters its login item (Apple, Service Management, SMAppService: mainApp, unregister()).
unregister=SUROGATE_DEV_LOGIN_ITEM

# The bundle's main processes, by their program: an Apple event's quit would wait on the terminal's Automation grant.
running() {
  ps -axo pid=,command= | awk -v program="$program" '{ pid = $1; sub(/^ *[0-9]+ /, ""); if (index($0, program) == 1) print pid }'
}

# The bundle started to take out its login item alone, within 15 s: whether it did.
unregistered() {
  [ -f "$app/Contents/Resources/app/login-item.mjs" ] || return 1
  env "$unregister=off" "$program" &
  local child=$!
  for _ in $(seq 1 30); do
    kill -0 "$child" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$child" 2>/dev/null; then
    kill -KILL "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
    return 1
  fi
  wait "$child"
}

if [ "${1:-}" = "--remove" ]; then
  pids=$(running)
  if [ -n "$pids" ]; then
    # SIGTERM is the app's own quit (Electron's shutdown on SIGTERM): its chats' work and its VM stop first, and it
    # asks before it stops a chat still at work.
    kill -TERM $pids 2>/dev/null || true
    for _ in $(seq 1 120); do
      [ -z "$(running)" ] && break
      sleep 0.5
    done
    [ -z "$(running)" ] || { echo "Surogate Dev is still running: quit it from its menu, then run this again" >&2; exit 1; }
  fi
  if [ -x "$program" ]; then
    if unregistered; then
      echo "Took out its login item"
    else
      echo "Its login item was not taken out: turn Surogate Dev off in System Settings, General, Login Items" >&2
    fi
  fi
  [ -d "$app" ] && "$lsregister" -u "$app" || true
  rm -rf "$app"
  echo "Removed $app"
  exit 0
fi

[ -f dist/shell/main.js ] || { echo "Build it first: npm run build:darwin" >&2; exit 1; }
[ -f dictionaries/en-US-10-1.bdic ] || { echo "Build it first: npm run build:darwin, which fetches the spelling dictionary" >&2; exit 1; }
electron="node_modules/electron/dist/Electron.app"
[ -d "$electron" ] || { echo "Install Electron first: npm ci && npm run electron:install" >&2; exit 1; }

mkdir -p "$HOME/Applications"
rm -rf "$app"
ditto "$electron" "$app"
plist="$app/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string ai.invergent.surogate.development "$plist"
plutil -replace CFBundleName -string Surogate "$plist"
plutil -replace CFBundleDisplayName -string "Surogate Dev" "$plist"
plutil -replace CFBundleURLTypes -json '[{"CFBundleURLName":"Surogate link","CFBundleURLSchemes":["surogate"]}]' "$plist"
# Electron runs the app folder in its Resources before its own default app: this checkout's main. Started with
# $unregister=off, as --remove starts it, the module before it takes out the bundle's login item (Electron's
# app.setLoginItemSettings) and ends the process, before anything of the app's runs.
mkdir -p "$app/Contents/Resources/app"
printf '{ "name": "surogate-desktop", "type": "module", "main": "main.mjs" }\n' > "$app/Contents/Resources/app/package.json"
cat > "$app/Contents/Resources/app/login-item.mjs" <<EOF
import { app } from "electron";

if (process.env.$unregister === "off") {
  app.setLoginItemSettings({ openAtLogin: false });
  process.exit(app.getLoginItemSettings().openAtLogin ? 1 : 0);
}
EOF
printf 'import "./login-item.mjs";\nimport %s;\n' "$(node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$desktop/dist/shell/main.js")" > "$app/Contents/Resources/app/main.mjs"
codesign --force --deep --sign - "$app"
"$lsregister" -f "$app"
echo "Made $app"
open "$app"
