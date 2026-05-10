# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Minimal additions on top of upstream:
#   - zstd for Phase D profile sync compression
#   - Surogates OCI labels

ARG UPSTREAM=docker.io/onkernel/chromium-headful@sha256:b7c0151ee94df4b5a5873605389727db456dbd7c98da74efa29c40eb238ac573
FROM ${UPSTREAM}

LABEL org.opencontainers.image.title="surogates-agent-browser" \
      org.opencontainers.image.description="Surogates per-session agent browser image" \
      org.opencontainers.image.source="https://github.com/Invergent-ai/surogates" \
      org.opencontainers.image.base.name="docker.io/onkernel/chromium-headful"

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends zstd \
    && rm -rf /var/lib/apt/lists/*

USER kernel
