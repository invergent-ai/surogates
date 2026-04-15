# Surogates Worker — production image
#
# Runs the stateless agent harness loop. Includes Anthropic Sandbox
# Runtime (srt) for OS-level process isolation via bubblewrap + seccomp.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/surogates-worker:latest -f images/worker/Dockerfile .
#
# The build context is the repository root.

# ── Stage 1: Python dependencies ──────────────────────────────────────
FROM python:3.12-slim AS python-deps

WORKDIR /build

# Install uv for fast dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY surogates/ ./surogates/

# Build a wheel and install into a virtual env.
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python . \
    && uv pip install --python /opt/venv/bin/python uvloop

# ── Stage 2: Node.js + srt ───────────────────────────────────────────
FROM node:22-slim AS srt-build

RUN npm install -g @anthropic-ai/sandbox-runtime

# ── Stage 3: Final image ─────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="surogates-worker" \
      org.opencontainers.image.description="Surogates managed-agent worker with OS-level sandbox" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# ── OS packages ──────────────────────────────────────────────────────
# bubblewrap     — Linux user-namespace sandbox (filesystem isolation)
# socat          — Unix socket relay for srt network proxy bridging
# ripgrep        — Fast file search for srt mandatory deny path detection
# git            — Agent needs git for workspace operations
# curl           — Agent tool dependency (web_extract, health checks)
# jq             — Common CLI tool agents use
# openssh-client — Agent may need ssh for git operations
# tini           — Proper PID 1 for signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap \
        socat \
        ripgrep \
        git \
        curl \
        jq \
        openssh-client \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js runtime (for srt) ───────────────────────────────────────
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=srt-build /usr/local/lib/node_modules/@anthropic-ai/sandbox-runtime \
                      /usr/local/lib/node_modules/@anthropic-ai/sandbox-runtime
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js \
          /usr/local/bin/srt \
    && chmod +x /usr/local/bin/srt

# ── Python virtualenv ────────────────────────────────────────────────
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Application code ────────────────────────────────────────────────
COPY surogates/ /opt/surogates/surogates/
COPY pyproject.toml /opt/surogates/

# ── Non-root user ───────────────────────────────────────────────────
# The worker runs as a non-root user for defense-in-depth.
# bubblewrap requires user namespaces (enabled by default on most kernels).
RUN groupadd --gid 1000 surogates \
    && useradd --uid 1000 --gid surogates --create-home surogates \
    && mkdir -p /tmp/surogates/workspaces \
    && chown -R surogates:surogates /tmp/surogates

# ── Default srt sandbox config ──────────────────────────────────────
# Restrictive defaults: write only to workspace, block secrets,
# no network by default (tools use their own HTTP clients, not shell).
COPY images/worker/srt-settings.json /etc/surogates/srt-settings.json

# ── Config mount points ─────────────────────────────────────────────
# These directories are populated via K8s ConfigMap/Secret mounts.
RUN mkdir -p /etc/surogates/policies \
             /etc/surogates/skills \
             /etc/surogates/tools \
             /etc/surogates/mcp \
             /data/tenant-assets \
    && chown -R surogates:surogates /data/tenant-assets

USER surogates
WORKDIR /opt/surogates

ENTRYPOINT ["tini", "--"]
CMD ["surogates", "worker"]
