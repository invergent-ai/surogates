#!/bin/bash
set -euo pipefail

# Required env vars:
#   AWS_ACCESS_KEY_ID     — S3 access key
#   AWS_SECRET_ACCESS_KEY — S3 secret key
#   S3_BUCKET             — bucket name to mount
#   S3_ENDPOINT           — S3 endpoint URL (e.g. http://garage:3900)
#
# Optional:
#   S3_MOUNT_POINT        — mount path (default: /workspace)
#   S3_REGION             — S3 region (default: garage)

MOUNT_POINT="${S3_MOUNT_POINT:-/workspace}"
REGION="${S3_REGION:-garage}"

if [ -z "${S3_BUCKET:-}" ]; then
    echo "ERROR: S3_BUCKET is required"
    exit 1
fi

if [ -z "${S3_ENDPOINT:-}" ]; then
    echo "ERROR: S3_ENDPOINT is required"
    exit 1
fi

# Write credentials file.
echo "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" > /etc/passwd-s3fs
chmod 600 /etc/passwd-s3fs

echo "Mounting s3://${S3_BUCKET} at ${MOUNT_POINT} (endpoint: ${S3_ENDPOINT})"

# Run s3fs in foreground (-f) so the container stays alive.
exec s3fs "${S3_BUCKET}" "${MOUNT_POINT}" \
    -f \
    -o url="${S3_ENDPOINT}" \
    -o use_path_request_style \
    -o passwd_file=/etc/passwd-s3fs \
    -o allow_other \
    -o uid=1000 \
    -o gid=1000 \
    -o umask=0022 \
    -o endpoint="${REGION}"
