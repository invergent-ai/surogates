# geesefs sidecar for Surogates sandbox pods.
#
# FUSE-mounts an S3 bucket as a POSIX filesystem at /workspace.
# Runs as a sidecar container alongside the sandbox main container,
# sharing an emptyDir volume with Bidirectional mount propagation.
#
# Uses geesefs (https://github.com/yandex-cloud/geesefs), an actively
# maintained Yandex fork of goofys. Compared to goofys v0.24.0 (which we
# used previously) it adds: partial-write support (no download→modify→
# re-upload for in-place edits), faster cold readdir/stat, a configurable
# memory limit, and continued upstream maintenance. Local R2 benchmarks
# showed ~1.8× faster cold reads / directory listings and ~8.8× faster
# in-place overwrites with no regression on streaming or small-file PUT.
#
# Image name (surogates-s3fs) is retained for compatibility with the
# helm charts and surogates config defaults.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/surogates-s3fs:latest -f images/s3fs/Dockerfile .
#
# Requires privileged security context for FUSE.

FROM debian:bookworm-slim

ARG GEESEFS_VERSION=v0.43.7

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fuse \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && echo "user_allow_other" >> /etc/fuse.conf

RUN curl -fsSL -o /usr/local/bin/geesefs \
        "https://github.com/yandex-cloud/geesefs/releases/download/${GEESEFS_VERSION}/geesefs-linux-amd64" \
    && chmod +x /usr/local/bin/geesefs

RUN mkdir -p /workspace

COPY images/s3fs/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
