#!/usr/bin/env bash
# The guest image's key for an arch: a sha256 over the arch's name and everything that arch's
# build reads from this repository. That is images/sandbox/Dockerfile up to the cloud sandbox's
# own stage (the kernel pin and its hashes included) without the stages of the other arches, each
# named <stage>-<arch>; each file the guest's stages COPY; and build.sh, which makes the published
# files and their manifest. So each arch's image has a key of its own, and a change to one arch's
# kernel stage is a new key for that arch alone. The release publishes the image under
# desktop/vm/<key>/ only when no image has that key yet. The bases, apt, pip and npm are not
# pinned: the Dockerfile's GUEST_REVISION, raised, takes what they give today into a new key.
#
# Usage:
#   images/guest/inputs.sh [amd64|arm64]   # the key of that arch's image: amd64's unless it says arm64
#   images/guest/inputs.sh --files         # the files hashed beside the Dockerfile's text
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# What the guest's stages read from the repository: a host test keeps this in step with
# the Dockerfile's COPY and ADD lines, and its RUNs' bind mounts, before the sandbox stage.
FILES=(
  images/guest/build.sh
  images/guest/kernel-btf.sh
  images/guest/kernel-config.sh
  images/guest/kernel-image.sh
  images/guest/surogate-init
  images/sandbox/pip-wrapper
  desktop/vm/rule.bpf.c
  desktop/vm/rule-match.h
)
# The arches the guest is built for, as Docker and Debian name them.
ARCHES=(amd64 arm64)

if [ "${1:-}" = "--files" ]; then
  printf '%s\n' "${FILES[@]}"
  exit 0
fi
ARCH="${1:-amd64}"
case " ${ARCHES[*]} " in
  *" $ARCH "*) [ "$#" -le 1 ] ;;
  *) false ;;
esac || { echo "usage: images/guest/inputs.sh [amd64|arm64] | --files" >&2; exit 2; }

# The Dockerfile's text that the arch's build reads: before the cloud sandbox's stage, and
# without each stage that is another arch's.
dockerfile() {
  sed '/^FROM tools AS sandbox$/,$d' images/sandbox/Dockerfile | awk -v arch="$ARCH" -v arches="${ARCHES[*]}" '
    BEGIN { n = split(arches, all, " ") }
    /^FROM / { skip = 0; if ($(NF - 1) == "AS") for (i = 1; i <= n; i++) if (all[i] != arch && $NF ~ ("-" all[i] "$")) skip = 1 }
    !skip'
}

{
  printf 'arch %s\n' "$ARCH"
  printf '%s  images/sandbox/Dockerfile\n' "$(dockerfile | sha256sum | cut -d' ' -f1)"
  sha256sum "${FILES[@]}"
} | sha256sum | cut -d' ' -f1
