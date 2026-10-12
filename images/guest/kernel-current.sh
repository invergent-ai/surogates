#!/usr/bin/env bash
# Fails while noble-security carries a newer hardware-enablement kernel than the
# guest's pin (images/sandbox/Dockerfile) for the arch: the guest kernel is the only
# boundary between two chats' commands, so a desktop release does not ship with a kernel
# that has a security update. The pin's update is the Dockerfile's: each arch's four .debs
# and their hashes, and the rule's hooks checked against the new BTF.
#
# Usage:
#   images/guest/kernel-current.sh [--arch amd64|arm64]    # amd64 unless it says arm64
# Environment: UBUNTU_SECURITY, amd64's archive (https://security.ubuntu.com/ubuntu), and
# UBUNTU_PORTS, arm64's (https://ports.ubuntu.com/ubuntu-ports).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ARCH=amd64
[ "${1:-}" != --arch ] || { ARCH="${2:-}"; shift 2 || true; }
case "$ARCH" in
  # Its words name the arch for arm64's alone.
  amd64) ARCHIVE="${UBUNTU_SECURITY:-https://security.ubuntu.com/ubuntu}"; FOR=""; NAMED="" ;;
  arm64) ARCHIVE="${UBUNTU_PORTS:-https://ports.ubuntu.com/ubuntu-ports}"; FOR=" for arm64"; NAMED="arm64 " ;;
  *) ARCH="" ;;
esac
[ -n "$ARCH" ] && [ "$#" -eq 0 ] || { echo "usage: images/guest/kernel-current.sh [--arch amd64|arm64]" >&2; exit 2; }
META=linux-image-generic-hwe-24.04

# The pin's version, from its image .deb's name: linux-image-<abi>-generic_<version>_<arch>.deb.
pinned="$(sed -n "s|.*/linux-image-[^_]*-generic_\\([^_]*\\)_${ARCH}\\.deb .*|\\1|p" "$REPO_ROOT/images/sandbox/Dockerfile")"
[ -n "$pinned" ] || { echo "kernel-current.sh: no kernel pin$FOR in images/sandbox/Dockerfile" >&2; exit 1; }
dpkg --validate-version "$pinned" 2> /dev/null \
  || { echo "kernel-current.sh: images/sandbox/Dockerfile's kernel pin$FOR is not one version dpkg can compare" >&2; exit 1; }
# Every version the index lists of it: one a stanza, in no order to rely on.
listed="$(curl -q -fsS --connect-timeout 30 --speed-limit 1024 --speed-time 60 --max-time 600 "$ARCHIVE/dists/noble-security/main/binary-$ARCH/Packages.xz" | xz -dc \
  | awk -v want="$META" '$1 == "Package:" { name = $2 } $1 == "Version:" && name == want { print $2 }')"
[ -n "$listed" ] || { echo "kernel-current.sh: noble-security lists no $META$FOR" >&2; exit 1; }
# A version dpkg cannot parse fails the check: dpkg only warns, and may answer either way.
for current in $listed; do
  unclear="kernel-current.sh: dpkg cannot compare noble-security's $META $current$FOR with the guest's pin $pinned"
  dpkg --validate-version "$current" 2> /dev/null || { echo "$unclear" >&2; exit 1; }
  compared=0
  dpkg --compare-versions "$current" gt "$pinned" || compared=$?
  case "$compared" in
    0)
      echo "kernel-current.sh: noble-security carries $META $current$FOR, newer than the guest's pin $pinned: pin it in images/sandbox/Dockerfile" >&2
      exit 1
      ;;
    1) ;;
    *) echo "$unclear" >&2; exit 1 ;;
  esac
done
echo "the guest's ${NAMED}kernel $pinned is noble-security's current one"
