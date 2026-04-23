# Surogates Sandbox — agent execution environment
#
# Includes the surogates Python package so the real tool handlers
# (terminal, file_ops, code_execution, etc.) run inside the sandbox
# with full feature parity.
#
# Build (from repo root):
#   docker build -t ghcr.io/invergent-ai/surogates-agent-sandbox:latest -f images/sandbox/Dockerfile .
# Load into k3d cluster (optional, if using k3d):
#   k3d image import ghcr.io/invergent-ai/surogates-agent-sandbox:latest -c surogates

# ── Stage 1: Python dependencies ──────────────────────────────────────
FROM python:3.12-slim AS python-deps

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY surogates/ ./surogates/

RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python . \
    && uv pip install --python /opt/venv/bin/python \
        pandas \
        numpy \
        openpyxl \
        xlsxwriter \
        xlrd \
        pyyaml \
        python-dateutil \
        requests \
        httpx \
        beautifulsoup4 \
        lxml \
        pillow \
        matplotlib \
        tabulate \
        pypdf \
        python-docx

# ── Stage 2: Final image ─────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="surogates-agent-sandbox" \
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

# Enable user site-packages so `pip install` packages are visible.
# Venvs disable this by default; removing the flag re-enables it.
RUN sed -i 's/include-system-site-packages = false/include-system-site-packages = true/' /opt/venv/pyvenv.cfg \
    || true

# ── uv + pip wrapper ────────────────────────────────────────────────
# Ship uv in the final image so `pip install` (via wrapper) is 10-100x
# faster. The wrapper preserves PIP_USER persistence to /workspace/.local
# by translating --user into uv's --target.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY images/sandbox/pip-wrapper /opt/venv/bin/pip
RUN chmod +x /usr/local/bin/uv /opt/venv/bin/pip \
    && ln -sf /opt/venv/bin/pip /opt/venv/bin/pip3

# PYTHONUSERBASE → /workspace/.local so pip installs persist on the
# s3fs-mounted workspace (not the ephemeral home dir).
ENV PATH="/workspace/.local/bin:/opt/venv/bin:$PATH" \
    PYTHONUSERBASE="/workspace/.local" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_USER=1

# ── Tool executor ───────────────────────────────────────────────────
COPY images/sandbox/tool-executor /usr/local/bin/tool-executor
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
