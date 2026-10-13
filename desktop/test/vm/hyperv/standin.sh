#!/usr/bin/env bash
# For the tests alone: the modules of QEMU's vsock device, from the image's guest-standin target, into
# <image>/standin, where the stand-in for Hyper-V's devices takes them for its initrd (standin.ts). From the
# same Dockerfile, kernel and build cache as the image in <image> (images/guest/build.sh): never published.
#
# Usage:
#   desktop/test/vm/hyperv/standin.sh [<image>]   # images/guest/out unless it names another
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
IMAGE="${1:-$REPO_ROOT/images/guest/out}"
docker build --platform linux/amd64 --target guest-standin --output "type=local,dest=$IMAGE/standin" \
  --file "$REPO_ROOT/images/sandbox/Dockerfile" "$REPO_ROOT"
ls "$IMAGE/standin"
