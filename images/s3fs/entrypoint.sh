#!/bin/bash
set -euo pipefail

# Required env vars (legacy / non-fleet mode):
#   AWS_ACCESS_KEY_ID     — S3 access key
#   AWS_SECRET_ACCESS_KEY — S3 secret key
#   S3_BUCKET_PATH        — bucket or bucket:/path to mount
#   S3_ENDPOINT           — S3 endpoint URL (e.g. http://garage:3900)
#
# Optional:
#   S3_MOUNT_POINT        — mount path (default: /workspace)
#   S3_REGION             — S3 region (default: derived from endpoint)
#
# Fleet mode (warm-pool warm pods):
#   FLEET_MODE=1          — block until /etc/fleet/ready exists, then
#                            source /etc/fleet/s3-creds.env for the
#                            session-scoped credentials. The sidecar
#                            writes WORKSPACE_SOURCE_REF=s3://bucket/prefix/
#                            and (optionally) AWS_S3_ENDPOINT into that file.
#   FLEET_CONFIG_DIR      — defaults to /etc/fleet.
#   WORKSPACE_PATH        — defaults to /workspace.

MOUNT_POINT="${S3_MOUNT_POINT:-${WORKSPACE_PATH:-/workspace}}"

if [ "${FLEET_MODE:-0}" = "1" ]; then
    FLEET_CONFIG_DIR="${FLEET_CONFIG_DIR:-/etc/fleet}"
    READY_FILE="${FLEET_CONFIG_DIR}/ready"
    CREDS_FILE="${FLEET_CONFIG_DIR}/s3-creds.env"
    echo "[s3fs-entrypoint] fleet mode: waiting for ${READY_FILE}..."
    # Unbounded wait — warm pods can sit idle in the pool for hours or
    # days before a lease arrives. The pod-level activeDeadlineSeconds
    # (set on the warm-pod manifest, default 3600s) is the outer
    # circuit breaker; the manager's reaper independently destroys
    # stuck pods past their deadline. A bounded wait here would cause
    # the s3fs container to exit and the pod to enter Error/Failed,
    # which the manager then has to clean up — extra churn for no win.
    while [ ! -f "${READY_FILE}" ]; do
        sleep 0.5
    done
    echo "[s3fs-entrypoint] config ready"
    # shellcheck disable=SC1090
    . "${CREDS_FILE}"

    # The sidecar's config writer uses environment variable names that
    # mirror the AWS SDK conventions (AWS_S3_ENDPOINT,
    # AWS_DEFAULT_REGION). Translate them to the legacy
    # S3_BUCKET_PATH / S3_ENDPOINT / S3_REGION inputs that the rest of
    # this script consumes, so the geesefs invocation below is reused.
    if [ -z "${WORKSPACE_SOURCE_REF:-}" ]; then
        echo "[s3fs-entrypoint] WORKSPACE_SOURCE_REF missing in creds file" >&2
        exit 1
    fi
    case "${WORKSPACE_SOURCE_REF}" in
        s3://*)
            rest="${WORKSPACE_SOURCE_REF#s3://}"
            BUCKET="${rest%%/*}"
            PREFIX="${rest#*/}"
            # Three cases:
            #   s3://bucket          → rest=bucket,        PREFIX==rest    → whole bucket
            #   s3://bucket/         → rest=bucket/,       PREFIX=""       → whole bucket
            #   s3://bucket/prefix/  → rest=bucket/prefix/, PREFIX=prefix/ → bucket:/prefix
            if [ -z "${PREFIX}" ] || [ "${PREFIX}" = "${rest}" ]; then
                S3_BUCKET_PATH="${BUCKET}"
            else
                # Strip trailing slash so the bucket:/prefix form is uniform.
                PREFIX="${PREFIX%/}"
                S3_BUCKET_PATH="${BUCKET}:/${PREFIX}"
            fi
            ;;
        *)
            echo "[s3fs-entrypoint] WORKSPACE_SOURCE_REF must start with s3://: '${WORKSPACE_SOURCE_REF}'" >&2
            exit 1
            ;;
    esac
    S3_ENDPOINT="${AWS_S3_ENDPOINT:-${S3_ENDPOINT:-}}"
    S3_REGION="${AWS_DEFAULT_REGION:-${S3_REGION:-}}"
fi

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

# Callers (historically targeting s3fs) pass BUCKET:/PREFIX. geesefs (like
# goofys before it) expects BUCKET:PREFIX with no leading slash on the
# prefix, so strip it.
BUCKET_SPEC="${S3_BUCKET_PATH/:\//:}"

# Sidecar containers typically have a ~256MB RAM budget; geesefs's default
# memory limit of 1000MB is unrealistic for our deployment shape.
GEESEFS_MEMORY_LIMIT_MB="${GEESEFS_MEMORY_LIMIT_MB:-256}"

# On-disk data cache. Speeds up repeated reads and lets writes coalesce
# locally before being flushed to S3. The path must be on a writable
# volume *outside* the FUSE mount (which would be circular). The sandbox
# pod manifest mounts an emptyDir at /var/cache/geesefs for this purpose;
# set GEESEFS_CACHE_DIR="" to disable.
GEESEFS_CACHE_DIR="${GEESEFS_CACHE_DIR-/var/cache/geesefs}"
GEESEFS_CACHE_ARGS=()
if [ -n "${GEESEFS_CACHE_DIR}" ]; then
    mkdir -p "${GEESEFS_CACHE_DIR}"
    GEESEFS_CACHE_ARGS=(--cache "${GEESEFS_CACHE_DIR}")
fi

echo "Mounting s3://${S3_BUCKET_PATH} at ${MOUNT_POINT} (endpoint: ${S3_ENDPOINT}, region: ${S3_REGION}, cache: ${GEESEFS_CACHE_DIR:-off})"

# In legacy mode we exec geesefs so it owns PID 1 and Kubernetes signals
# reach it directly. In fleet mode we need to write the .s3fs-mounted
# sentinel into the mount point *after* the mount lands, which is only
# possible if we keep our shell alive long enough to write the file —
# so we run geesefs in the background, write the sentinel, and trap
# SIGTERM/SIGINT to forward them to it.
if [ "${FLEET_MODE:-0}" = "1" ]; then
    geesefs \
        --endpoint "${S3_ENDPOINT}" \
        --region "${S3_REGION}" \
        -o allow_other \
        --uid 1000 \
        --gid 1000 \
        --file-mode 0644 \
        --dir-mode 0755 \
        --memory-limit "${GEESEFS_MEMORY_LIMIT_MB}" \
        "${GEESEFS_CACHE_ARGS[@]}" \
        -f \
        "${BUCKET_SPEC}" "${MOUNT_POINT}" &
    GEESEFS_PID=$!

    # mountpoint(1) needs util-linux; the base image already includes it.
    # Poll for up to 30 s — beyond that geesefs has clearly failed and the
    # manager's pod_ready_timeout will tear the pod down.
    for _ in $(seq 1 150); do
        if mountpoint -q "${MOUNT_POINT}"; then
            touch "${MOUNT_POINT}/.s3fs-mounted"
            echo "[s3fs-entrypoint] mount confirmed; sentinel written"
            break
        fi
        if ! kill -0 "${GEESEFS_PID}" 2>/dev/null; then
            echo "[s3fs-entrypoint] geesefs exited before mount; aborting" >&2
            wait "${GEESEFS_PID}" || true
            exit 1
        fi
        sleep 0.2
    done

    # Forward signals so K8s teardown / sidecar /release shuts geesefs
    # down cleanly and unmounts the fuse layer.
    trap 'kill -TERM "${GEESEFS_PID}" 2>/dev/null || true; wait "${GEESEFS_PID}" || true; exit 0' TERM INT
    wait "${GEESEFS_PID}"
    exit $?
fi

# Legacy mode: exec geesefs directly.
exec geesefs \
    --endpoint "${S3_ENDPOINT}" \
    --region "${S3_REGION}" \
    -o allow_other \
    --uid 1000 \
    --gid 1000 \
    --file-mode 0644 \
    --dir-mode 0755 \
    --memory-limit "${GEESEFS_MEMORY_LIMIT_MB}" \
    "${GEESEFS_CACHE_ARGS[@]}" \
    -f \
    "${BUCKET_SPEC}" "${MOUNT_POINT}"
