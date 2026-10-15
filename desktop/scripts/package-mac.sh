#!/usr/bin/env bash
# The macOS app, Surogate.app, laid out as a person drags it to Applications and as an update swaps it in: Electron's own
# macOS build renamed as Electron's "Application Distribution" renames one, with the app beside it, and the app's native
# programs where the app's code looks for them (src/helper-path.ts, src/vm/darwin.ts, src/hosts/tool-hosts.ts,
# src/clean-child.ts, src/files/fsat.ts).
#
#   Surogate.app/Contents/
#     Info.plist                    Electron's, with the keys of macos/info-plist.json in place of its own (Surogate's
#                                   names and identifier, ai.invergent.surogate, the version, the icon, the oldest macOS,
#                                   the surogate:// scheme and the privacy prompts' words), and no asar's hash
#     MacOS/Surogate                Electron, renamed, with the app's fuses (no inspector)
#     MacOS/surogate-vm             the VM's program (native/build.sh), and surogate-helper (native/helper/build.sh)
#     Frameworks/                   Electron's frameworks, and its four helper apps renamed Surogate Helper, (GPU),
#                                   (Renderer) and (Plugin), each with its program and its own identifier
#     Resources/app/                the app as a folder, never an asar: dist/ (no testing/), with fsat.node in
#                                   dist/native/; assets/, dictionaries/, package.json at <version>, the production
#                                   node_modules, with none of srt's Linux or Windows programs, which the macOS app
#                                   never runs; and bin/: the app's node (scripts/node.sh darwin-arm64), its rg
#                                   (scripts/rg.sh) and surogate-spawn (native/spawn/build.sh)
#     Resources/vm/                 agent.img, the agent disk, and the guest image's manifest, the arm64 image's
#     Resources/release-keys        install.sh's release keys, as the app reads them (scripts/release-keys.mjs), a file of
#                                   mode 0644
#     Resources/surogate.icns       the app's icon, Surogate's mark, in place of Electron's
#
# Signed ad hoc, inside out, so that it runs on the Mac that built it and on the runner (scripts/sign-mac.sh sign); the
# release signs it again with its own identity.
#
# Usage, on a Mac with Apple silicon, after npm ci and scripts/mac-build.sh, from any folder, with its paths as that
# folder names them:
#   scripts/package-mac.sh <version> <vm manifest.json> <agent disk> <out>
# It writes <out>/Surogate.app, replacing one that was there. The agent disk is vm/agent-disk.sh's, which the release
# builds on Linux, and the VM manifest the arm64 guest image's, the one the macOS app boots.
set -euo pipefail

VERSION="${1:-}"
VM_MANIFEST="${2:-}"
AGENT_DISK="${3:-}"
OUT="${4:-}"
# Whether $1 is a version as a release names one: x.y.z in the ten digits, with no zero before a part.
a_version() {
  local LC_ALL=C
  [[ "$1" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
}
a_version "$VERSION" && [ -f "$VM_MANIFEST" ] && [ -f "$AGENT_DISK" ] && [ -n "$OUT" ] && [ "$#" -eq 4 ] \
  || { echo "usage: scripts/package-mac.sh <x.y.z> <vm manifest.json> <agent disk> <out>" >&2; exit 2; }
[ "$(uname -s)-$(uname -m)" = Darwin-arm64 ] \
  || { echo "package-mac.sh lays out the macOS app on a Mac with Apple silicon, whose Electron npm installed" >&2; exit 1; }
# Its paths are its caller's, read from where it was called, before it works from this package's folder.
whole() { (cd "$(dirname "$1")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$1")"); }
VM_MANIFEST="$(whole "$VM_MANIFEST")"
AGENT_DISK="$(whole "$AGENT_DISK")"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
cd "$(dirname "$0")/.."
node -e 'const { arch } = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8")); process.exit(arch === "arm64" ? 0 : 1)' "$VM_MANIFEST" 2>/dev/null \
  || { echo "package-mac.sh: $VM_MANIFEST is not the manifest of an arm64 guest image, the one the macOS app boots" >&2; exit 1; }
# The app's own node, by its pin, in place of the one a development build copies from the PATH.
scripts/node.sh darwin-arm64
for part in dist/shell/main.js dist/native/fsat.node dictionaries bin/node bin/rg bin/surogate-spawn native/out/surogate-vm native/helper/.build/surogate-helper; do
  [ -e "$part" ] || { echo "package-mac.sh: $part is missing: run scripts/mac-build.sh first" >&2; exit 1; }
done

# Staged beside <out>, on its volume: the bundle is moved into place in one rename.
stage="$(mktemp -d "$OUT/.package-mac.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
top="$stage/Surogate.app"
contents="$top/Contents"
app="$contents/Resources/app"

ditto node_modules/electron/dist/Electron.app "$top"
mv "$contents/MacOS/Electron" "$contents/MacOS/Surogate"
# Electron's own default app, and its icon: a packaged Electron loads Resources/app instead.
rm "$contents/Resources/default_app.asar" "$contents/Resources/electron.icns"
cp macos/surogate.icns "$contents/Resources/surogate.icns"
plist="$contents/Info.plist"
# The hash Electron keeps of the default app's asar, which is gone: the app is a folder.
plutil -remove ElectronAsarIntegrity "$plist"
# Each key of macos/info-plist.json, the version put in, a line each: its name, and a string as it is or any other value
# as its JSON.
node -e '
  const keys = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
  for (const [key, value] of Object.entries(keys)) {
    const given = typeof value === "string" ? ["string", value] : ["json", JSON.stringify(value)];
    console.log([key, given[0], given[1].replaceAll("@VERSION@", process.argv[2])].join("\t"));
  }
' macos/info-plist.json "$VERSION" | while IFS="$(printf '\t')" read -r key kind value; do
  plutil -replace "$key" "-$kind" "$value" "$plist"
done
# Electron's four helper apps, each Surogate's by its name, its program's and its identifier: Electron starts the helper
# named after the app's own CFBundleName.
for kind in "" " (GPU)" " (Renderer)" " (Plugin)"; do
  helper="$contents/Frameworks/Surogate Helper$kind.app"
  mv "$contents/Frameworks/Electron Helper$kind.app" "$helper"
  mv "$helper/Contents/MacOS/Electron Helper$kind" "$helper/Contents/MacOS/Surogate Helper$kind"
  suffix="$(printf '%s' "$kind" | tr -d ' ()' | tr '[:upper:]' '[:lower:]')"
  plutil -replace CFBundleName -string "Surogate Helper$kind" "$helper/Contents/Info.plist"
  plutil -replace CFBundleExecutable -string "Surogate Helper$kind" "$helper/Contents/Info.plist"
  plutil -replace CFBundleIdentifier -string "ai.invergent.surogate.helper${suffix:+.$suffix}" "$helper/Contents/Info.plist"
done
# The app's fuses, on Electron's framework, where Electron reads them on macOS.
node scripts/fuses.mjs "$top"

mkdir -p "$app/bin" "$contents/Resources/vm"
cp -R dist assets dictionaries "$app/"
# The echo client and the sign-in helper are the tests' own; the agent disk ships in Resources/vm.
rm -rf "$app/dist/testing" "$app/dist/agent.img"
find "$app/dist" -name '*.map' -delete
node -e 'const fs = require("node:fs"); const p = JSON.parse(fs.readFileSync("package.json", "utf8")); p.version = process.argv[1]; fs.writeFileSync(process.argv[2], `${JSON.stringify(p, null, 2)}\n`)' "$VERSION" "$app/package.json"
cp package-lock.json "$app/"
(cd "$app" && NPM_CONFIG_USERCONFIG=/dev/null npm ci --omit=dev --ignore-scripts --prefer-offline --no-audit --no-fund --loglevel=error)
rm "$app/package-lock.json"
rm -rf "$app/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp" "$app/node_modules/@anthropic-ai/sandbox-runtime/vendor/srt-win"

cp bin/node bin/rg bin/surogate-spawn "$app/bin/"
cp native/out/surogate-vm native/helper/.build/surogate-helper "$contents/MacOS/"
cp "$AGENT_DISK" "$contents/Resources/vm/agent.img"
cp "$VM_MANIFEST" "$contents/Resources/vm/manifest.json"
node scripts/release-keys.mjs release/install.sh "$contents/Resources/release-keys"
# A file only its owner writes: the app takes its keys only so.
chmod 0644 "$contents/Resources/release-keys"

# Nothing in it writable but by its owner, whatever umask built it, and nothing with a set-id or sticky bit.
chmod -R u+w,go-w,a+rX,a-st "$top"
scripts/sign-mac.sh sign "$top" - >/dev/null

rm -rf "$OUT/Surogate.app"
mv "$top" "$OUT/Surogate.app"
echo "$OUT/Surogate.app"
