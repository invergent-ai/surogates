#!/usr/bin/env bash
# What the measurements of the macOS VM need, made once on the Mac that runs them. Each measurement
# is a file of this folder, run on its own; each says what it found, then PASS or FAIL for each check,
# and writes its findings to out/<name>.json with this Mac's model, chip and macOS.
#
# First, on a Mac with Apple silicon and macOS 14 or later, in a user account made for this:
#   - Xcode 16 or later (its Swift 6 toolchain): xcode-select --install is not enough for its tests;
#   - Node 22.14.0 first on the PATH (nvm install 22.14.0);
#   - Docker Desktop, or another Docker with arm64 Linux containers (OrbStack, colima), running;
#   - Homebrew's zstd (brew install zstd).
# Then, from desktop/:
#   npm ci && npm run electron:install
#   test/vm/darwin/measure/prepare.sh
# It builds dist/, surogate-vm (native/build.sh), the arm64 guest image into images/guest/out (unless
# one is there, or SUROGATE_VM_IMAGE names one), and the agent disks in a Linux container: the app's,
# and one for the measurements whose init also runs, as the guest's root, a probe a measurement leaves
# in a shared folder (.measure-probe). That one is never the app's: it is made in probing/ here, never
# in dist/.
set -euo pipefail

DESKTOP="$(cd "$(dirname "$0")/../../../.." && pwd)"
REPO="$(cd "$DESKTOP/.." && pwd)"
IMAGE="${SUROGATE_VM_IMAGE:-$REPO/images/guest/out}"

say() { printf 'prepare: %s\n' "$*"; }
need() { command -v "$1" >/dev/null || { say "$1 is missing: $2"; exit 1; }; }

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || { say "this is for a Mac with Apple silicon"; exit 1; }
need swift "install Xcode 16 or later"
need node "put Node 22.14.0 first on the PATH"
need docker "install and start Docker Desktop, OrbStack or colima"
need zstd "brew install zstd"
[ "$(node --version)" = v22.14.0 ] || say "Node is $(node --version), not v22.14.0: the measurements may still run"
docker info >/dev/null 2>&1 || { say "Docker is installed but not running: start it"; exit 1; }

cd "$DESKTOP"
[ -d node_modules ] || { say "run npm ci && npm run electron:install first"; exit 1; }
say "building dist/"
npx tsc -p tsconfig.build.json
say "building surogate-vm"
native/build.sh

if [ -f "$IMAGE/vmlinuz" ] && [ -f "$IMAGE/rootfs.img" ]; then
  say "the guest image is there already: $IMAGE"
else
  say "building the arm64 guest image into $IMAGE (an hour or so the first time)"
  mkdir -p "$IMAGE"
  docker build --platform linux/arm64 --target guest-disk --output "type=local,dest=$IMAGE" --file "$REPO/images/sandbox/Dockerfile" "$REPO"
  for file in rootfs.img vmlinuz; do zstd -q -d -f --sparse "$IMAGE/$file.zst" -o "$IMAGE/$file"; done
  printf '{"arch":"arm64"}\n' > "$IMAGE/manifest.json"
fi

# The agent disks: mke2fs, debugfs and fakeroot are Linux's, so a container makes them, from this checkout.
say "making the agent disks"
docker run --rm --platform linux/arm64 -v "$REPO:/repo" -w /repo/desktop ubuntu:24.04 bash -euc '
  apt-get update -qq && apt-get install -y -qq --no-install-recommends e2fsprogs fakeroot > /dev/null
  vm/agent-disk.sh dist/agent.img
  # As the VM tests make one with an init of their own: the script and enter-root linked, so it finds this checkout.
  tree=$(mktemp -d) && mkdir "$tree/vm" && ln -s /repo/desktop/dist "$tree/dist"
  ln -s /repo/desktop/vm/agent-disk.sh /repo/desktop/vm/enter-root "$tree/vm/"
  awk "FNR == NR { probe = probe \$0 \"\\n\"; next } /^exec \\/usr\\/bin\\/tini/ { printf \"%s\", probe } { print }" test/vm/darwin/measure/probe.sh vm/init > "$tree/vm/init"
  chmod 755 "$tree/vm/init" && grep -q measure-probe "$tree/vm/init"
  # Out of dist/, which the app is built from: the probing disk is never in an app.
  mkdir -p test/vm/darwin/measure/probing
  "$tree/vm/agent-disk.sh" test/vm/darwin/measure/probing/agent.img
  chown "$(stat -c %u:%g dist)" dist/agent.img test/vm/darwin/measure/probing test/vm/darwin/measure/probing/agent.img
'
say "ready: each measurement runs from desktop/ as"
say "  npx vitest run -c test/vm/darwin/measure/vitest.config.ts <name>"
say "in the order boot, escape, ports, disks, shares, coherence, owners, mmap, descriptors, readonly, tcc, network, lifetime, sleep, memory"
