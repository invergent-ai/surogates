# s3fs-fuse sidecar for Surogates sandbox pods.
#
# FUSE-mounts an S3 bucket as a POSIX filesystem at /workspace.
# Runs as a sidecar container alongside the sandbox main container,
# sharing an emptyDir volume with Bidirectional mount propagation.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/s3fs-fuse:latest -f k8/images/s3fs/Dockerfile .
#
# Requires privileged security context for FUSE.

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        s3fs \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && echo "user_allow_other" >> /etc/fuse.conf

# Create mount point.
RUN mkdir -p /workspace

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
