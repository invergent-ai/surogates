# surogates voice: answers phone calls (LiveKit SIP) with the agent their number belongs to.
#
# Its own image, not the worker's: the voice extra (LiveKit agents, Silero VAD, numpy/scipy, PyAV)
# is heavy and only this process needs it, while the worker needs the sandbox tooling this one
# never uses. Same Python build as the worker image otherwise.

# ── Stage 1: Python dependencies ─────────────────────────────────────
FROM python:3.12-slim AS python-deps

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml ./
COPY surogates/ ./surogates/

RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python ".[voice]" \
    && uv pip install --python /opt/venv/bin/python uvloop

# ── Stage 2: Runtime ─────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="surogates-voice" \
      org.opencontainers.image.description="Surogates phone voice worker (LiveKit SIP)" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# tini: PID 1, forwards signals so a pod stop ends calls cleanly
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY surogates/ /opt/surogates/surogates/
COPY pyproject.toml /opt/surogates/

# the background sound pack is downloaded from ops into ~/.cache and kept between calls
RUN groupadd --gid 1000 surogates \
    && useradd --uid 1000 --gid surogates --create-home surogates \
    && mkdir -p /home/surogates/.cache/surogates \
    && chown -R surogates:surogates /home/surogates/.cache

USER surogates
WORKDIR /opt/surogates

# health: SUROGATES_VOICE_HEALTH_PORT (8003)
EXPOSE 8003

ENTRYPOINT ["tini", "--"]
CMD ["surogates", "voice"]
