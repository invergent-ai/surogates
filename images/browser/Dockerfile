# Surogates Agent Browser — kernel-images-derived per-session browser pod.
#
# Additions on top of upstream onkernel/chromium-headful:
#   - zstd for profile sync compression
#   - neko disabled — its WebRTC live view needs UDP/TURN the cluster cannot
#     provide, and the browser shell streams the page over CDP instead, so
#     nothing in this image serves a remote view of the desktop
#   - Surogates OCI labels

ARG UPSTREAM=docker.io/onkernel/chromium-headful@sha256:b7c0151ee94df4b5a5873605389727db456dbd7c98da74efa29c40eb238ac573
FROM ${UPSTREAM}

LABEL org.opencontainers.image.title="surogates-agent-browser" \
      org.opencontainers.image.description="Surogates agent browser image" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates" \
      org.opencontainers.image.base.name="docker.io/onkernel/chromium-headful"

ENV CHROMIUM_FLAGS="--no-sandbox"

USER root

# zstd for profile sync. neko is forced off: the shell reads the page over
# CDP, so no service in this image exposes the X display remotely.
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
    && sed -i 's/^autostart=.*$/autostart=false/' \
        /etc/supervisor/conf.d/services/neko.conf \
    && rm -rf /var/lib/apt/lists/*

# A fresh browser must open on a blank page. Chromium is launched with no
# start URL, so it opens a new tab -- and upstream's managed policy points
# NewTabPageLocation at start.duckduckgo.com, which is what both the agent
# and the viewer then see. Patched rather than layered as a second policy
# file: Chromium merges every file in the managed directory and resolving a
# conflicting key across two of them comes down to filename order.
#
# The DuckDuckGo *search provider* is deliberately left alone -- that is what
# a typed query in the address bar should use; only the landing page changes.
RUN python3 - <<'PY'
import json

path = "/etc/chromium/policies/managed/policy.json"
with open(path) as handle:
    policy = json.load(handle)

policy.update({
    "NewTabPageLocation": "about:blank",
    "HomepageLocation": "about:blank",
    "HomepageIsNewTabPage": False,
    # 4 = "open a specific set of URLs" on startup.
    "RestoreOnStartup": 4,
    "RestoreOnStartupURLs": ["about:blank"],
})

with open(path, "w") as handle:
    json.dump(policy, handle, indent=2)
PY

# Keep the upstream runtime user. The headful wrapper starts supervisord as root
# and the Chromium launcher drops privileges internally with runuser.
