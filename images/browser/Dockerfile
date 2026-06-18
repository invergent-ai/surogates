# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Additions on top of upstream onkernel/chromium-headful:
#   - zstd for profile sync compression
#   - x11vnc + websockify: RFB-over-WebSocket live view on :8080
#   - neko disabled — its WebRTC live view needs UDP/TURN the cluster cannot
#     provide; x11vnc attaches to the existing Xorg :1 and websockify bridges it
#     to the :8080 WebSocket the harness already proxies as live_view_url
#   - Surogates OCI labels

ARG UPSTREAM=docker.io/onkernel/chromium-headful@sha256:b7c0151ee94df4b5a5873605389727db456dbd7c98da74efa29c40eb238ac573
FROM ${UPSTREAM}

LABEL org.opencontainers.image.title="surogates-agent-browser" \
      org.opencontainers.image.description="Surogates agent browser image" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates" \
      org.opencontainers.image.base.name="docker.io/onkernel/chromium-headful"

ENV CHROMIUM_FLAGS="--no-sandbox"

USER root

# zstd for profile sync; x11vnc + websockify for the RFB-over-WebSocket live
# view. neko is forced off so it cannot contend for :8080 (websockify binds it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends zstd x11vnc websockify \
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
    && sed -i 's/^autostart=.*$/autostart=false/' \
        /etc/supervisor/conf.d/services/neko.conf \
    && rm -rf /var/lib/apt/lists/*

COPY images/browser/supervisor/x11vnc.conf /etc/supervisor/conf.d/services/x11vnc.conf
COPY images/browser/supervisor/websockify.conf /etc/supervisor/conf.d/services/websockify.conf

# Keep the upstream runtime user. The headful wrapper starts supervisord as root
# and the Chromium launcher drops privileges internally with runuser.
