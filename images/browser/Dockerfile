# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Minimal additions on top of upstream:
#   - zstd for Phase D profile sync compression
#   - Surogates OCI labels

ARG UPSTREAM=docker.io/onkernel/chromium-headful@sha256:b7c0151ee94df4b5a5873605389727db456dbd7c98da74efa29c40eb238ac573
FROM ${UPSTREAM}

LABEL org.opencontainers.image.title="surogates-agent-browser" \
      org.opencontainers.image.description="Surogates agent browser image" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates" \
      org.opencontainers.image.base.name="docker.io/onkernel/chromium-headful"

ENV CHROMIUM_FLAGS="--no-sandbox"

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends zstd \
    && mkdir -p /var/log/supervisord \
        /etc/envoy/certs \
        /usr/local/share/ca-certificates \
        /run/dbus \
        /recordings \
    && touch /var/log/supervisord.log /kernel-envoy-proxy.crt \
    && chown -R kernel:kernel \
        /var/log/supervisord \
        /var/log/supervisord.log \
        /etc/envoy/certs \
        /usr/local/share/ca-certificates \
        /etc/ssl/certs \
        /run \
        /recordings \
        /kernel-envoy-proxy.crt \
    && rm -rf /var/lib/apt/lists/*

# Keep the upstream runtime user. The headful wrapper starts supervisord as root
# and the Chromium launcher drops privileges internally with runuser.
