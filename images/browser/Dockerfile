# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Minimal additions on top of upstream:
#   - zstd for Phase D profile sync compression
#   - Surogates branding for the embedded Neko live-view shell
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
    && sed -i 's/^autostart=false$/autostart=true/' \
        /etc/supervisor/conf.d/services/neko.conf \
    && rm -rf /var/lib/apt/lists/*

COPY logo.svg /var/www/img/logo.b42d8ab7.svg

RUN sed -i \
        -e 's#<title>Kernel</title>#<title>Surogates Browser</title>#' \
        -e 's|<meta name="theme-color" content="#81B300">|<meta name="theme-color" content="#FFB000">|' \
        -e 's|<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,[^"]*">|<link rel="icon" type="image/svg+xml" href="img/logo.b42d8ab7.svg">|' \
        /var/www/index.html \
    && chown kernel:kernel /var/www/img/logo.b42d8ab7.svg /var/www/index.html

# Keep the upstream runtime user. The headful wrapper starts supervisord as root
# and the Chromium launcher drops privileges internally with runuser.
