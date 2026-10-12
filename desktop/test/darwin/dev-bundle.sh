#!/usr/bin/env bash
# A development build of the app as a Mac app of its own, for the checks a Mac's person makes by hand:
# ~/Applications/Surogate Dev.app, this package's Electron with the surogate:// scheme in its Info.plist
# (CFBundleURLTypes), which Launch Services reads, and a bundle identifier of its own, so that its login
# item, its Keychain item and its privacy grants are never the released app's. Its code is this
# checkout's dist/, as `npm run build:darwin` built it; its executable keeps Electron's name, so the app
# runs as a development build (app.isPackaged is false). Signed ad hoc, as development builds are.
#
#   test/darwin/dev-bundle.sh            make it, register it with Launch Services, and open it
#   test/darwin/dev-bundle.sh --remove   quit it, take it out of Launch Services, and remove it
set -euo pipefail

cd "$(dirname "$0")/../.."
desktop=$(pwd -P)
app="$HOME/Applications/Surogate Dev.app"
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

if [ "${1:-}" = "--remove" ]; then
  osascript -e 'tell application id "ai.invergent.surogate.development" to quit' 2>/dev/null || true
  [ -d "$app" ] && "$lsregister" -u "$app" || true
  rm -rf "$app"
  echo "Removed $app"
  exit 0
fi

[ -f dist/shell/main.js ] || { echo "Build it first: npm run build:darwin" >&2; exit 1; }
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
# Electron runs the app folder in its Resources before its own default app: this checkout's main.
mkdir -p "$app/Contents/Resources/app"
printf '{ "name": "surogate-desktop", "type": "module", "main": "main.mjs" }\n' > "$app/Contents/Resources/app/package.json"
printf 'import %s;\n' "$(node -e 'process.stdout.write(JSON.stringify(process.argv[1]))' "$desktop/dist/shell/main.js")" > "$app/Contents/Resources/app/main.mjs"
codesign --force --deep --sign - "$app"
"$lsregister" -f "$app"
echo "Made $app"
open "$app"
