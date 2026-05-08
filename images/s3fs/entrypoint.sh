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
#   S3_REGION             — S3 region (default: garage)

MOUNT_POINT="${S3_MOUNT_POINT:-/workspace}"

if [ -z "${S3_BUCKET_PATH:-}" ]; then
    echo "ERROR: S3_BUCKET_PATH is required"
    exit 1
fi

if [ -z "${S3_ENDPOINT:-}" ]; then
    echo "ERROR: S3_ENDPOINT is required"
    exit 1
fi

# Derive a sensible region for s3fs's `-o endpoint=` (SigV4 signing label)
# when the caller didn't set one explicitly.  AWS S3 hosts encode the
# region in the hostname; mismatching it makes AWS reject the pre-mount
# service check with HTTP 400 and s3fs exits cleanly.
if [ -z "${S3_REGION:-}" ]; then
    HOST="$(echo "$S3_ENDPOINT" | sed -E 's|^[a-z]+://([^/:]+).*|\1|i')"
    case "$HOST" in
        s3.*.amazonaws.com|s3-*.amazonaws.com)
            S3_REGION="$(echo "$HOST" | sed -E 's|^s3[.-]([a-z0-9-]+)\.amazonaws\.com$|\1|')"
            ;;
        *)                              S3_REGION="eu-central-1" ;;
    esac
fi
REGION="$S3_REGION"

# Write credentials file.
echo "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" > /etc/passwd-s3fs
chmod 600 /etc/passwd-s3fs

echo "Mounting s3://${S3_BUCKET_PATH} at ${MOUNT_POINT} (endpoint: ${S3_ENDPOINT})"

# Run s3fs in foreground (-f) so the container stays alive.
exec s3fs "${S3_BUCKET_PATH}" "${MOUNT_POINT}" \
    -f \
    -o url="${S3_ENDPOINT}" \
    -o use_path_request_style \
    -o passwd_file=/etc/passwd-s3fs \
    -o allow_other \
    -o uid=1000 \
    -o gid=1000 \
    -o umask=0022 \
    -o endpoint="${REGION}"
