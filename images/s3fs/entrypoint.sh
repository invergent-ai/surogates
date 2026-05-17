#!/bin/bash
set -euo pipefail

# Required env vars:
#   AWS_ACCESS_KEY_ID     — S3 access key
#   AWS_SECRET_ACCESS_KEY — S3 secret key
#   S3_BUCKET_PATH        — bucket or bucket:/path to mount
#   S3_ENDPOINT           — S3 endpoint URL (e.g. http://garage:3900)
#
# Optional:
#   S3_MOUNT_POINT        — mount path (default: /workspace)
#   S3_REGION             — S3 region (default: derived from endpoint)

MOUNT_POINT="${S3_MOUNT_POINT:-/workspace}"

if [ -z "${S3_BUCKET_PATH:-}" ]; then
    echo "ERROR: S3_BUCKET_PATH is required"
    exit 1
fi

if [ -z "${S3_ENDPOINT:-}" ]; then
    echo "ERROR: S3_ENDPOINT is required"
    exit 1
fi

# Derive a sensible region for --region (SigV4 signing label) when the caller
# didn't set one explicitly. AWS S3 hosts encode the region in the hostname;
# mismatching it makes AWS reject the pre-mount service check with HTTP 400.
if [ -z "${S3_REGION:-}" ]; then
    HOST="$(echo "$S3_ENDPOINT" | sed -E 's|^[a-z]+://([^/:]+).*|\1|i')"
    case "$HOST" in
        s3.*.amazonaws.com|s3-*.amazonaws.com)
            S3_REGION="$(echo "$HOST" | sed -E 's|^s3[.-]([a-z0-9-]+)\.amazonaws\.com$|\1|')"
            ;;
        *)                              S3_REGION="eu-central-1" ;;
    esac
fi

# Callers (historically targeting s3fs) pass BUCKET:/PREFIX. goofys expects
# BUCKET:PREFIX (no leading slash on the prefix), so strip it.
BUCKET_SPEC="${S3_BUCKET_PATH/:\//:}"

echo "Mounting s3://${S3_BUCKET_PATH} at ${MOUNT_POINT} (endpoint: ${S3_ENDPOINT}, region: ${S3_REGION})"

# -f keeps goofys in the foreground so the container stays alive. goofys
# reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from the environment
# (already set via envFrom in the pod spec).
exec goofys \
    --endpoint "${S3_ENDPOINT}" \
    --region "${S3_REGION}" \
    -o allow_other \
    --uid 1000 \
    --gid 1000 \
    --file-mode 0644 \
    --dir-mode 0755 \
    -f \
    "${BUCKET_SPEC}" "${MOUNT_POINT}"
