#!/usr/bin/env bash
# Regression check: stopping the s3fs container must not leave a dead FUSE
# mount on the host while another container still holds /workspace open.
#
# Needs docker (privileged, /dev/fuse) and rclone on the host.
#   ./images/s3fs/test-teardown.sh            # builds surogates-s3fs:teardown-test
#   ./images/s3fs/test-teardown.sh <image>    # tests an existing image
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${1:-}"
if [ -z "$IMAGE" ]; then
    IMAGE=surogates-s3fs:teardown-test
    docker build -q -t "$IMAGE" -f "$REPO_ROOT/images/s3fs/Dockerfile" "$REPO_ROOT" >/dev/null
fi

TMP="$(mktemp -d)"
WS="$TMP/ws"; PORT=19911
mkdir -p "$TMP/s3/ws" "$WS"
rclone serve s3 --addr "127.0.0.1:$PORT" "$TMP/s3" >/dev/null 2>&1 &
RCLONE_PID=$!

cleanup() {
    docker rm -f s3t-teardown s3h-teardown >/dev/null 2>&1 || true
    if grep -q " $WS fuse" /proc/mounts; then
        docker run --rm --privileged --pid=host --entrypoint nsenter "$IMAGE" \
            -t 1 -m -- umount -l "$WS" || true
    fi
    kill "$RCLONE_PID" 2>/dev/null || true
    rm -rf "$TMP"
}
trap cleanup EXIT

docker run -d --name s3t-teardown --privileged --network host --device /dev/fuse \
    -e AWS_ACCESS_KEY_ID=k -e AWS_SECRET_ACCESS_KEY=k -e S3_BUCKET_PATH=ws \
    -e S3_ENDPOINT="http://127.0.0.1:$PORT" -e S3_REGION=us-east-1 -e GEESEFS_CACHE_DIR= \
    -v "$WS:/workspace:rshared" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do grep -q " $WS fuse" /proc/mounts && break; sleep 0.5; done
grep -q " $WS fuse" /proc/mounts || { echo "FAIL: geesefs never mounted"; docker logs s3t-teardown; exit 1; }

# Stand-in for the sandbox container: its cwd keeps the mount busy.
docker run -d --name s3h-teardown --entrypoint sh -v "$WS:/workspace:rslave" "$IMAGE" \
    -c 'cd /workspace && exec sleep 1000' >/dev/null
sleep 1
docker stop -t 5 s3t-teardown >/dev/null

if grep -q " $WS fuse" /proc/mounts; then
    echo "FAIL: FUSE mount left on host after s3fs stopped"
    exit 1
fi
echo "PASS"
