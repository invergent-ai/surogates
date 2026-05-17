# goofys sidecar for Surogates sandbox pods.
#
# FUSE-mounts an S3 bucket as a POSIX filesystem at /workspace.
# Runs as a sidecar container alongside the sandbox main container,
# sharing an emptyDir volume with Bidirectional mount propagation.
#
# Uses goofys (https://github.com/kahing/goofys) instead of s3fs because
# s3fs makes ~1 S3 request per small file, which turned `git clone` of a
# 26 MB / 1000-file repo into a 3+ minute operation. Goofys batches and
# parallelises aggressively.
#
# Image name (surogates-s3fs) is retained for compatibility with the
# helm charts and surogates config defaults.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/surogates-s3fs:latest -f images/s3fs/Dockerfile .
#
# Requires privileged security context for FUSE.

FROM debian:bookworm-slim

ARG GOOFYS_VERSION=v0.24.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fuse \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && echo "user_allow_other" >> /etc/fuse.conf

RUN curl -fsSL -o /usr/local/bin/goofys \
        "https://github.com/kahing/goofys/releases/download/${GOOFYS_VERSION}/goofys" \
    && chmod +x /usr/local/bin/goofys

RUN mkdir -p /workspace

COPY images/s3fs/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]