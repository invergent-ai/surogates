#!/usr/bin/env bash
# The file host under Seatbelt, by hand on a Mac with Apple silicon: what the macOS runner cannot show. It builds what
# the file host runs on (surogate-spawn, fsat, the app's rg, a node at bin/node), runs the file host's macOS suites,
# then starts a host in srt's Seatbelt on each of a Mac's own places: a folder in ~/Documents (behind macOS's privacy
# consent), an exFAT volume of a disk image (a USB drive's filesystem), and iCloud Drive where it is signed in. Run it
# from a clone of the repository, in a macOS user account made for it, never your own:
#
#   desktop/test/acceptance/macos/file-host.sh
#
# Needs Xcode's command line tools (xcode-select --install) and Node 22.14 first on the PATH. macOS asks, the first
# time, whether the terminal may reach the Documents folder (and iCloud Drive): allow it. It prints each step, then the
# measurements the suites write out, then PASS, or FAIL and the steps that failed. It leaves nothing in the places.
set -uo pipefail

DESKTOP="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$DESKTOP"
[ "$(uname -s)-$(uname -m)" = Darwin-arm64 ] || { echo "file-host.sh runs on a Mac with Apple silicon" >&2; exit 2; }
echo "macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion)), $(sysctl -n machdep.cpu.brand_string), node $(node --version)"

failed=()
step() {
  local name="$1"
  shift
  echo "== $name"
  if "$@"; then echo "-- $name: ok"; else echo "-- $name: FAILED"; failed+=("$name"); fi
}

build() {
  npm ci --no-audit --no-fund >/dev/null && npm run electron:install >/dev/null && npx tsc -p tsconfig.build.json \
    && native/spawn/build.sh && native/fsat/build.sh && mkdir -p bin && cp "$(command -v node)" bin/node && scripts/rg.sh
}
step "build what the file host runs on" build
export PATH="$DESKTOP/bin:$PATH"
log="$(mktemp -d)"

step "the file host's macOS suites" sh -c "npx vitest run --project macos > '$log/suites.log' 2>&1"

places=()
documents="$HOME/Documents/surogate-file-host-probe"
mkdir -p "$documents" && places+=("$documents")
image="$log/probe.dmg"
volume=""
mkdir -p "$log/volume"
if hdiutil create -quiet -size 64m -fs ExFAT -volname SurogateProbe "$image" && hdiutil attach -quiet -nobrowse -mountpoint "$log/volume" "$image"; then
  volume="$log/volume"
  places+=("$volume")
fi
icloud="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
[ -d "$icloud" ] && mkdir -p "$icloud/surogate-file-host-probe" && places+=("$icloud/surogate-file-host-probe")
joined="$(IFS=:; echo "${places[*]}")"
echo "places: $joined"
step "the file host on a Mac's own places" sh -c "SUROGATE_FILE_HOST_PLACES='$joined' npx vitest run --project unit test/file-host-places.test.ts > '$log/places.log' 2>&1"

[ -n "$volume" ] && hdiutil detach -quiet "$volume"
rm -rf "$documents" "$icloud/surogate-file-host-probe" 2>/dev/null

echo "== what the suites measured"
grep -h -E '^(Seatbelt|O_SEARCH|M8|/)' "$log/suites.log" "$log/places.log" || true
echo "== the logs: $log/suites.log, $log/places.log"
if [ ${#failed[@]} -eq 0 ]; then
  echo PASS
else
  echo "FAIL: ${failed[*]}"
  grep -h -E '^ +(×|FAIL)' "$log/suites.log" "$log/places.log" | head -40
  exit 1
fi
