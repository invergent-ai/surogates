# Surogates executor bundle — the sandbox's tool executor for a session's own image
#
# A session created with config.sandbox_image runs in that image (a task's repository
# with its toolchain), which has none of the agent-sandbox image's executor. This image
# holds everything the executor needs under /opt/surogates, self-contained:
#
#   bin/python3   a relocatable CPython 3.12 (python-build-standalone, through uv) with
#                 the surogates package and its locked dependencies, wheels for
#                 manylinux_2_28 (glibc 2.28 or later: asyncpg ships no older wheel), byte-compiled ahead (the tree
#                 is mounted read-only, so nothing is ever compiled at a start)
#   bin/tini      static, the container's init
#   bin/rg        ripgrep, which the file search tool runs (task images have none)
#
# The Docker sandbox backend mounts /opt/surogates of this image read-only into the
# session's image (`--mount type=image`, Docker 28 or later) and starts
# `/opt/surogates/bin/tini -- /opt/surogates/bin/python3 -I -m surogates.sandbox.executor_server`
# there, as the image's own user. Nothing of it is on the commands' PATH.
#
# Build (from repo root), the tag sandbox.executor_bundle_image names:
#   docker build -t ghcr.io/invergent-ai/surogates-executor-bundle:latest -f images/executor-bundle/Dockerfile .

# ── tini and ripgrep, by architecture, checked by hash ───────────────
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS static-amd64
ADD --checksum=sha256:c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee \
    https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-amd64 /static/tini
ADD --checksum=sha256:4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e \
    https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz /tmp/rg.tar.gz

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS static-arm64
ADD --checksum=sha256:eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9 \
    https://github.com/krallin/tini/releases/download/v0.19.0/tini-static-arm64 /static/tini
ADD --checksum=sha256:c827481c4ff4ea10c9dc7a4022c8de5db34a5737cb74484d62eb94a95841ab2f \
    https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-unknown-linux-gnu.tar.gz /tmp/rg.tar.gz

FROM static-${TARGETARCH} AS static
RUN mkdir -p /tmp/rg && tar -xzf /tmp/rg.tar.gz -C /tmp/rg --strip-components=1 \
    && mv /tmp/rg/rg /static/rg && chown root:root /static/tini /static/rg && chmod 0755 /static/tini /static/rg \
    && rm -rf /tmp/rg /tmp/rg.tar.gz

# ── The interpreter and the surogates package ────────────────────────
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS build
ARG TARGETARCH
COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PYTHON_INSTALL_DIR=/opt/surogates/python \
    UV_NO_CACHE=1
RUN uv python install --no-bin 3.12 \
    && py=$(uv python find --managed-python 3.12) \
    && mkdir -p /opt/surogates/bin \
    && ln -s "../${py#/opt/surogates/}" /opt/surogates/bin/python3
COPY pyproject.toml uv.lock /build/
COPY surogates/ /build/surogates/
# The locked dependencies as wheels for the oldest glibc we serve, checked against uv.lock's
# hashes; the project itself last, without them. A dependency with no such wheel fails the
# build here, not a session.
RUN case "$TARGETARCH" in \
        amd64) platform=x86_64-manylinux_2_28 ;; \
        arm64) platform=aarch64-manylinux_2_28 ;; \
        *) echo "no executor bundle for $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && cd /build \
    && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --python /opt/surogates/bin/python3 --break-system-packages \
        --python-platform "$platform" --only-binary :all: -r /tmp/requirements.txt \
    && uv pip install --python /opt/surogates/bin/python3 --break-system-packages --no-deps /build \
    && rm -rf /build /tmp/requirements.txt
COPY --from=static /static/ /opt/surogates/bin/
# Byte-compiled with hashes that are never checked against the sources' times: the tree is
# read-only where it runs. A few dependencies ship Python 2 files that never import (aenum's
# _py2.py), so compileall's status is not the test: the executor and every tool it registers must import,
# from the bundle alone.
RUN (/opt/surogates/bin/python3 -m compileall -qq -j 4 --invalidation-mode unchecked-hash /opt/surogates/python || true) \
    && /opt/surogates/bin/python3 -I -c "from surogates.sandbox.executor_server import init_registry; init_registry()" \
    && /opt/surogates/bin/python3 -I -c "import json, platform, sys; json.dump({'python': platform.python_version(), 'glibc_floor': '2.28', 'arch': platform.machine()}, open('/opt/surogates/bundle.json', 'w'))"

# ── The bundle ──────────────────────────────────────────────────────
# On a Debian base, not scratch, so that a Kubernetes init container can copy the tree
# where an image volume is not available.
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS bundle
LABEL org.opencontainers.image.title="surogates-executor-bundle" \
      org.opencontainers.image.description="The Surogates tool executor, self-contained, for a session's own sandbox image" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"
COPY --from=build /opt/surogates /opt/surogates
