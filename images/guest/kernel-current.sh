#!/usr/bin/env bash
# Fails while noble-security carries a newer hardware-enablement kernel than the
# guest's pin (images/sandbox/Dockerfile): the guest kernel is the only boundary
# between two chats' commands, so a desktop release does not ship with a kernel
# that has a security update. The pin's update is the Dockerfile's: four .debs
# and their hashes, and the rule's hooks checked against the new BTF.
#
# Usage:
#   images/guest/kernel-current.sh
# Environment: UBUNTU_SECURITY, the archive (https://security.ubuntu.com/ubuntu).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ARCHIVE="${UBUNTU_SECURITY:-https://security.ubuntu.com/ubuntu}"
META=linux-image-generic-hwe-24.04

# The pin's version, from its image .deb's name: linux-image-<abi>-generic_<version>_amd64.deb.
pinned="$(sed -n 's|.*/linux-image-[^_]*-generic_\([^_]*\)_amd64\.deb .*|\1|p' "$REPO_ROOT/images/sandbox/Dockerfile")"
[ -n "$pinned" ] || { echo "kernel-current.sh: no kernel pin in images/sandbox/Dockerfile" >&2; exit 1; }
# Every version the index lists of it: one a stanza, in no order to rely on.
listed="$(curl -q -fsS --connect-timeout 30 --speed-limit 1024 --speed-time 60 --max-time 600 "$ARCHIVE/dists/noble-security/main/binary-amd64/Packages.xz" | xz -dc \
  | awk -v want="$META" '$1 == "Package:" { name = $2 } $1 == "Version:" && name == want { print $2 }')"
[ -n "$listed" ] || { echo "kernel-current.sh: noble-security lists no $META" >&2; exit 1; }
for current in $listed; do
  if dpkg --compare-versions "$current" gt "$pinned"; then
    echo "kernel-current.sh: noble-security carries $META $current, newer than the guest's pin $pinned: pin it in images/sandbox/Dockerfile" >&2
    exit 1
  fi
done
echo "the guest's kernel $pinned is noble-security's current one"
