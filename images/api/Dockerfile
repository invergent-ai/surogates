# Surogates API Gateway — production image
#
# Runs the FastAPI server (sessions, events SSE, auth, admin) and serves
# the built web SPA.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/surogates-api:latest -f images/api/Dockerfile .
#
# The build context is the repository root.

# ── Stage 1: Web SPA build ───────────────────────────────────────────
FROM node:22-slim AS web-build

WORKDIR /build
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build

# ── Stage 2: Final image ─────────────────────────────────────────────
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

# ── Web SPA ──────────────────────────────────────────────────────────
# Bundled alongside the python package so create_app() can resolve it
# via Path(__file__).parent.parent / "web" / "dist".
COPY --from=web-build /build/dist /opt/surogates/surogates/web/dist

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
