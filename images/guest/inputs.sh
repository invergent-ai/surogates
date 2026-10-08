#!/usr/bin/env bash
# The guest image's key: a sha256 over everything its build reads from this
# repository. That is images/sandbox/Dockerfile up to the cloud sandbox's own stage
# (the kernel pin and its hashes included), each file the guest's stages COPY, and
# build.sh, which makes the published files and their manifest. The release
# publishes the image under desktop/vm/<key>/ only when no image has that key yet.
# The bases, apt, pip and npm are not pinned: the Dockerfile's GUEST_REVISION, raised,
# takes what they give today into a new key.
#
# Usage:
#   images/guest/inputs.sh            # the key
#   images/guest/inputs.sh --files    # the files hashed beside the Dockerfile's text
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# What the guest's stages read from the repository: a host test keeps this in step with
# the Dockerfile's COPY and ADD lines, and its RUNs' bind mounts, before the sandbox stage.
FILES=(
  images/guest/build.sh
  images/guest/surogate-init
  images/sandbox/pip-wrapper
  desktop/vm/rule.bpf.c
  desktop/vm/rule-match.h
)

if [ "${1:-}" = "--files" ]; then
  printf '%s\n' "${FILES[@]}"
  exit 0
fi

{
  printf '%s  images/sandbox/Dockerfile\n' "$(sed '/^FROM tools AS sandbox$/,$d' images/sandbox/Dockerfile | sha256sum | cut -d' ' -f1)"
  sha256sum "${FILES[@]}"
} | sha256sum | cut -d' ' -f1
