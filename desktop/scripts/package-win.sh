#!/usr/bin/env bash
# The Windows app, laid out as the MSI installs it into %ProgramFiles%\Surogate (windows/msi/Surogate.wxs): Electron's
# own Windows build with the app beside it, as Electron's "Application Distribution" lays one out, and the app's native
# programs where the app's code looks for them (src/helper-path.ts, src/vm/win32.ts, src/hosts/tool-hosts.ts).
#
#   Surogate\
#     Surogate.exe                  Electron, renamed, with Surogate's icon and version resource (scripts/win-exe.mjs)
#                                   and the app's fuses (no inspector); Electron's DLLs and files beside it
#     resources\app\                the app as a folder, never an asar: dist\ (no testing\), with ntat.node in
#                                   dist\native\; assets\, dictionaries\, package.json at <version>, the production
#                                   node_modules, and bin\: the app's node.exe (scripts/node.sh, as nodejs.org signed
#                                   it) and rg.exe, which the file host runs and the MSI's firewall rules block
#     resources\vm\                 agent.img, the agent disk given its VHD footer, and the guest image's manifest
#     resources\release-keys        install.sh's release keys, as the app reads them (scripts/release-keys.mjs)
#     bin\                          surogate-vm.exe and surogate-helper.exe, the user's side of the VM and the app's helper
#     service\                      surogate-service.exe, the Surogate VM service, LocalSystem, which the MSI installs
#
# srt has no part in the Windows app (src/hosts/srt-win32.ts): its own Windows programs are left out of node_modules, so
# that no program ships that the app never runs. Nothing here is signed: the release signs every program but node.exe.
#
# Usage, on Windows on x64, in Git for Windows' bash, after npm run electron:install and npm run build:win32, from any
# folder, with its paths as that folder names them:
#   scripts/package-win.sh <version> <vm manifest.json> <agent disk> <out>
# It writes <out>\Surogate\, replacing one that was there. The agent disk is vm/agent-disk.sh's, which the release
# builds on Linux, and the VM manifest the amd64 guest image's, the one the Windows app boots.
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
  || { echo "usage: scripts/package-win.sh <x.y.z> <vm manifest.json> <agent disk> <out>" >&2; exit 2; }
case "$(uname -s)-$(uname -m)" in
  MINGW64_NT*-x86_64 | MSYS_NT*-x86_64) ;;
  *) echo "package-win.sh lays out the Windows app on Windows on x64, whose Electron npm installed" >&2; exit 1 ;;
esac
# Its paths are its caller's, read from where it was called, before it works from this package's folder.
VM_MANIFEST="$(realpath "$VM_MANIFEST")"
AGENT_DISK="$(realpath "$AGENT_DISK")"
OUT="$(realpath -m "$OUT")"
cd "$(dirname "$0")/.."
node -e 'const { arch } = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8")); process.exit(arch === "amd64" ? 0 : 1)' "$VM_MANIFEST" 2>/dev/null \
  || { echo "package-win.sh: $VM_MANIFEST is not the manifest of an amd64 guest image, the one the Windows app boots" >&2; exit 1; }
# The app's own node, by its pin, in place of the one a development build copies from the PATH.
scripts/node.sh
BUILT=native/windows/target/release
for part in dist/native/ntat.node bin/node.exe bin/rg.exe "$BUILT/surogate-vm.exe" "$BUILT/surogate-helper.exe" "$BUILT/surogate-service.exe"; do
  [ -f "$part" ] || { echo "package-win.sh: $part is missing: run npm run build:win32 first" >&2; exit 1; }
done

# Staged beside <out>, on its volume: the layout is moved into place in one rename.
mkdir -p "$OUT"
stage="$(mktemp -d -p "$OUT")"
trap 'rm -rf "$stage"' EXIT
top="$stage/Surogate"
app="$top/resources/app"

cp -a node_modules/electron/dist "$top"
mv "$top/electron.exe" "$top/Surogate.exe"
# Electron's own default app: a packaged Electron loads resources\app instead.
rm "$top/resources/default_app.asar"
# Its resources before its fuses: each rewrites the program, and the fuses are read from the program as it ships.
node scripts/win-exe.mjs "$top/Surogate.exe" "$VERSION" windows/surogate.ico
node scripts/fuses.mjs "$top/Surogate.exe"

mkdir -p "$app" "$top/resources/vm" "$top/bin" "$top/service"
cp -a dist assets bin dictionaries "$app/"
# The echo client and the sign-in helper are the tests' own; the agent disk ships in resources\vm.
rm -rf "$app/dist/testing" "$app/dist/agent.img"
find "$app/dist" -name '*.map' -delete
node -e 'const fs = require("node:fs"); const p = JSON.parse(fs.readFileSync("package.json", "utf8")); p.version = process.argv[1]; fs.writeFileSync(process.argv[2], `${JSON.stringify(p, null, 2)}\n`)' "$VERSION" "$app/package.json"
cp package-lock.json "$app/"
(cd "$app" && NPM_CONFIG_USERCONFIG=/dev/null npm ci --omit=dev --ignore-scripts --prefer-offline --no-audit --no-fund --loglevel=error)
rm "$app/package-lock.json"
rm -rf "$app/node_modules/@anthropic-ai/sandbox-runtime/vendor/srt-win"

cp "$BUILT/surogate-vm.exe" "$BUILT/surogate-helper.exe" "$top/bin/"
cp "$BUILT/surogate-service.exe" "$top/service/"
cp "$AGENT_DISK" "$top/resources/vm/agent.img"
node scripts/vhd-footer.mjs "$top/resources/vm/agent.img"
cp "$VM_MANIFEST" "$top/resources/vm/manifest.json"
node scripts/release-keys.mjs release/install.sh "$top/resources/release-keys"

rm -rf "$OUT/Surogate"
mv "$top" "$OUT/Surogate"
echo "$OUT/Surogate"
