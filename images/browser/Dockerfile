# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Minimal additions on top of upstream:
#   - zstd for Phase D profile sync compression
#   - lower-cost Neko live-view capture defaults
#   - Surogates branding for the embedded Neko live-view shell
#   - Surogates OCI labels

ARG UPSTREAM=docker.io/onkernel/chromium-headful@sha256:289b10f4bb3d3dcc84bf49feae09a50b350838d78d97eef5eeeb812ac439ff6a
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
    && sed -i 's/screen: "1920x1080@25"/screen: "1280x720@10"/' \
        /etc/neko/neko.yaml \
    && rm -rf /var/lib/apt/lists/*

COPY images/browser/logo.svg /var/www/img/logo.surogates.svg

RUN cp /var/www/img/logo.surogates.svg /var/www/img/logo.b42d8ab7.svg \
    && cp /var/www/js/app.32920cec.js /var/www/js/app.surogates.js \
    && cp /var/www/js/app-legacy.5a69f29c.js /var/www/js/app-legacy.surogates.js \
    && sed -i 's|img/logo.b42d8ab7.svg|img/logo.surogates.svg|g' \
        /var/www/js/app.surogates.js \
        /var/www/js/app-legacy.surogates.js \
    && sed -i \
        -e 's#<title>Kernel</title>#<title>Surogates Browser</title>#' \
        -e 's|<meta name="theme-color" content="#81B300">|<meta name="theme-color" content="#FFB000">|' \
        -e 's|<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,[^"]*">|<link rel="icon" type="image/svg+xml" href="img/logo.b42d8ab7.svg">|' \
        -e 's|href="img/logo.b42d8ab7.svg"|href="img/logo.surogates.svg"|' \
        -e 's|src="js/app.32920cec.js"|src="js/app.surogates.js"|' \
        -e 's|src="js/app-legacy.5a69f29c.js"|src="js/app-legacy.surogates.js"|' \
        /var/www/index.html \
    && chown kernel:kernel \
        /var/www/img/logo.surogates.svg \
        /var/www/img/logo.b42d8ab7.svg \
        /var/www/js/app.surogates.js \
        /var/www/js/app-legacy.surogates.js \
        /var/www/index.html

# Keep the upstream runtime user. The headful wrapper starts supervisord as root
# and the Chromium launcher drops privileges internally with runuser.
