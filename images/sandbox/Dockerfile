# Surogates Sandbox — agent execution environment
#
# Includes the surogates Python package so the real tool handlers
# (terminal, file_ops, code_execution, etc.) run inside the sandbox
# with full feature parity.
#
# Build (from repo root):
#   docker build -t ghcr.io/invergent-ai/agent-sandbox:latest -f k8/images/sandbox/Dockerfile .
# Load into k3d cluster (optional, if using k3d):
#   k3d image import ghcr.io/invergent-ai/agent-sandbox:latest -c surogates

# ── Stage 1: Python dependencies ──────────────────────────────────────
FROM python:3.12-slim AS python-deps

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY surogates/ ./surogates/

RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python .

# ── Stage 2: Final image ─────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="agent-sandbox" \
      org.opencontainers.image.description="Surogates agent sandbox execution environment" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# ── OS packages ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        curl \
        jq \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── Python virtualenv ────────────────────────────────────────────────
COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Tool executor ───────────────────────────────────────────────────
COPY k8/images/sandbox/tool-executor /usr/local/bin/tool-executor
RUN chmod +x /usr/local/bin/tool-executor

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["tini", "--"]
CMD ["sleep", "infinity"]
