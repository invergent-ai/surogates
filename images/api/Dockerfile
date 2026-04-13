# Surogates API Gateway — production image
#
# Runs the FastAPI server (sessions, events SSE, auth, admin).
# No sandbox runtime needed — the API never executes tools.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/surogates-api:latest -f k8/images/api/Dockerfile .
#
# The build context is the repository root.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="surogates-api" \
      org.opencontainers.image.description="Surogates API gateway" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# ── OS packages ──────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ─────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /opt/surogates
COPY pyproject.toml ./
COPY surogates/ ./surogates/

RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python . \
    && uv pip install --python /opt/venv/bin/python uvloop

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd --gid 1000 surogates \
    && useradd --uid 1000 --gid surogates --create-home surogates

# ── Config mount points ─────────────────────────────────────────────
RUN mkdir -p /etc/surogates/policies \
    && chown -R surogates:surogates /etc/surogates

USER surogates

ENTRYPOINT ["tini", "--"]
CMD ["surogates", "api"]
