#!/usr/bin/env bash
# The file host under Seatbelt, by hand on a Mac with Apple silicon: what the macOS runner cannot show. It builds what
# the file host runs on (surogate-spawn, fsat, the app's rg, a node at bin/node) and surogate-helper, which the macos
# project's files run, runs the file host's macOS suites, then starts a host in srt's Seatbelt on each of a Mac's own
# places: a folder in ~/Documents (behind macOS's privacy consent), an exFAT volume of a disk image (a USB drive's
# filesystem), and iCloud Drive where it is signed in. Run it from a clone of the repository, in a macOS user account
# made for it, never your own:
#
#   desktop/test/acceptance/macos/file-host.sh
#
# Needs Xcode (swift, for surogate-helper) and Node 22.14 first on the PATH. macOS asks, the first time, whether the
# terminal may reach the Documents folder (and iCloud Drive): allow it. It prints each step, then the measurements the
# suites write out, then each place it could not make, with the words of what refused it. Its last line is PASS or
# FAIL and the steps that failed, and either way the places it skipped. It leaves nothing in the places.
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
    && native/spawn/build.sh && native/fsat/build.sh && native/helper/build.sh && scripts/mac-node.sh && scripts/rg.sh
}
step "build what the file host runs on" build
export PATH="$DESKTOP/bin:$PATH"
log="$(mktemp -d)"

# The file host's suites, as the macOS workflow's file-host job runs them, and the macos project's.
SUITES="test/clean-child.test.ts test/fd-paths-darwin.test.ts test/srt-darwin.test.ts test/folder-lock.test.ts test/boot-id.test.ts
  test/hosts-handed.test.ts test/policy-darwin.test.ts test/agent-disk-imports.test.ts test/flags.test.ts test/operations.test.ts
  test/hooks.test.ts test/hooks-chmod.test.ts test/land.test.ts test/file-tools-race.test.ts"
step "the file host's suites" sh -c "npx vitest run --project unit $(echo $SUITES) > '$log/suites.log' 2>&1"
step "the macos project" sh -c "npx vitest run --project macos >> '$log/suites.log' 2>&1"

places=()
# Each place that could not be made: its name, and the words of what refused it, on one line.
skipped=()
skip() {
  skipped+=("$1 ($(printf '%s' "$2" | tr '\n' ' ' | sed 's/  */ /g; s/ *$//'))")
}
documents="$HOME/Documents/surogate-file-host-probe"
if said=$(mkdir -p "$documents" 2>&1); then places+=("$documents"); else skip "the folder in Documents" "mkdir: $said"; fi
image="$log/probe.dmg"
volume=""
mkdir -p "$log/volume"
# hdiutil's own words, which -quiet would close: what refused the image, or its exit status where it said nothing.
said=$(hdiutil create -size 64m -fs ExFAT -volname SurogateProbe "$image" 2>&1)
made=$?
if [ "$made" -ne 0 ]; then
  skip "the exFAT disk image" "hdiutil create: ${said:-no words, exit $made}"
else
  said=$(hdiutil attach -nobrowse -mountpoint "$log/volume" "$image" 2>&1)
  attached=$?
  if [ "$attached" -ne 0 ]; then
    skip "the exFAT disk image" "hdiutil attach: ${said:-no words, exit $attached}"
  else
    volume="$log/volume"
    places+=("$volume")
  fi
fi
icloud="$HOME/Library/Mobile Documents/com~apple~CloudDocs"
if [ ! -d "$icloud" ]; then
  skip "iCloud Drive" "not signed in: no $icloud"
elif said=$(mkdir -p "$icloud/surogate-file-host-probe" 2>&1); then
  places+=("$icloud/surogate-file-host-probe")
else
  skip "iCloud Drive" "mkdir: $said"
fi
joined="$(IFS=:; echo "${places[*]:-}")"
echo "places: $joined"
step "the file host on a Mac's own places" sh -c "SUROGATE_FILE_HOST_PLACES='$joined' npx vitest run --project unit test/file-host-places.test.ts > '$log/places.log' 2>&1"

[ -n "$volume" ] && hdiutil detach -quiet "$volume"
rm -rf "$documents" "$icloud/surogate-file-host-probe" 2>/dev/null

echo "== what the suites measured"
grep -h -E '^(Seatbelt|O_SEARCH|the link-flip race|/)' "$log/suites.log" "$log/places.log" || true
echo "== the logs: $log/suites.log, $log/places.log"
echo "== the places it could not make"
if [ ${#skipped[@]} -eq 0 ]; then echo "none"; else printf '%s\n' "${skipped[@]}"; fi
names="none"
[ ${#skipped[@]} -eq 0 ] || names="$(printf '%s\n' "${skipped[@]}" | sed 's/ (.*//' | paste -sd, - | sed 's/,/, /g')"
if [ ${#failed[@]} -eq 0 ]; then
  echo "PASS; places skipped: $names"
else
  grep -h -E '^ +(×|FAIL)' "$log/suites.log" "$log/places.log" | head -40
  echo "FAIL: ${failed[*]}; places skipped: $names"
  exit 1
fi
