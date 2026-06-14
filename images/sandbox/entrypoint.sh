#!/bin/bash
# Sandbox container entrypoint.
#
# Default (and K8s, which overrides the command anyway): exec the tool-executor
# daemon, unchanged. When SANDBOX_S3FS_INLINE=1 (set only by the Docker sandbox
# backend in s3fs mode), first FUSE-mount the session's R2 prefix at /workspace
# via geesefs, then supervise the daemon so SIGTERM unmounts cleanly (geesefs
# flushes pending writes before the container dies).
#
# geesefs runs as the non-root `sandbox` user (uid 1000); fusermount needs that
# uid to have an /etc/passwd entry, which the image provides.
set -euo pipefail
WORKSPACE="${WORKSPACE_DIR:-/workspace}"

if [ "${SANDBOX_S3FS_INLINE:-0}" != "1" ]; then
    exec python -m surogates.sandbox.executor_server
fi

: "${S3_BUCKET_PATH:?required for inline s3fs}"
: "${S3_ENDPOINT:?required for inline s3fs}"
# geesefs wants bucket:prefix (no leading slash on the prefix).
bucket_spec="${S3_BUCKET_PATH/:\//:}"

geesefs --endpoint "$S3_ENDPOINT" --region "${S3_REGION:-auto}" \
        -o allow_other --uid 1000 --gid 1000 \
        --file-mode 0644 --dir-mode 0755 \
        --memory-limit "${GEESEFS_MEMORY_LIMIT_MB:-256}" \
        -f "$bucket_spec" "$WORKSPACE" &
gpid=$!
for _ in $(seq 1 150); do
    mountpoint -q "$WORKSPACE" && break
    kill -0 "$gpid" 2>/dev/null || { echo "[sandbox-entrypoint] geesefs exited before mount" >&2; wait "$gpid" || true; exit 1; }
    sleep 0.2
done
mountpoint -q "$WORKSPACE" || { echo "[sandbox-entrypoint] geesefs mount timed out" >&2; exit 1; }
echo "[sandbox-entrypoint] mounted $bucket_spec at $WORKSPACE"

python -m surogates.sandbox.executor_server &
dpid=$!
trap 'kill -TERM "$dpid" 2>/dev/null || true; wait "$dpid" 2>/dev/null || true; \
      fusermount -u "$WORKSPACE" 2>/dev/null || true; \
      kill -TERM "$gpid" 2>/dev/null || true; exit 0' TERM INT
wait "$dpid"
fusermount -u "$WORKSPACE" 2>/dev/null || true
kill -TERM "$gpid" 2>/dev/null || true
