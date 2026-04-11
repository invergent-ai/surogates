# Surogates Sandbox — agent execution environment
#
# Lightweight image with tool binaries. No application code — the
# sandbox receives commands from the worker via K8s exec API and
# runs them using the tool-executor script.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/agent-sandbox:latest -f k8/images/sandbox/Dockerfile .

FROM python:3.12-slim

LABEL org.opencontainers.image.title="agent-sandbox" \
      org.opencontainers.image.description="Surogates agent sandbox execution environment" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# ── OS packages ──────────────────────────────────────────────────────
# git       — workspace operations (clone, commit, push)
# ripgrep   — fast file search (search_files tool)
# curl      — web requests
# jq        — JSON processing
# tini      — proper PID 1 for signal handling
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        curl \
        jq \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── Tool executor ───────────────────────────────────────────────────
# Thin script that accepts tool name + JSON args, dispatches to the
# right implementation, and writes result JSON to stdout.
COPY tool-executor /usr/local/bin/tool-executor
RUN chmod +x /usr/local/bin/tool-executor

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["tini", "--"]
# The sandbox pod runs `sleep infinity` — the worker execs into it.
CMD ["sleep", "infinity"]
