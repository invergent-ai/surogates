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
        pyfiglet \
        nano-pdf \
        reportlab \
        pdfplumber \
        pypdfium2 \
        pytesseract \
        pdf2image
# liteparse, pypdf, python-docx, python-pptx, openpyxl are pulled in
# by the surogates package install above (project deps), so they are
# not listed here.
#
# The extra libraries above (reportlab, pdfplumber, pypdfium2,
# pytesseract, pdf2image) are referenced by the bundled productivity
# skills (pdf/docx/xlsx/pptx); preinstalling them lets the skills run
# without any `pip install` step.  Pandoc the binary comes from the
# apt layer below (the `pandoc` PyPI package is just a wrapper).

# ── Stage 2: Final image ─────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="surogates-agent-sandbox" \
      org.opencontainers.image.description="Surogates agent sandbox execution environment" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# ── OS packages ──────────────────────────────────────────────────────
# poppler-utils ships pdftotext/pdftoppm/pdfimages used by the pdf skill.
# qpdf + pdftk-java are command-line PDF manipulation tools the skill
# documents as alternatives.  pandoc is the actual binary referenced by
# the docx skill's "Reading Content (advanced)" recipe.  libreoffice is
# used by scripts/office/soffice.py (.doc → .docx conversion and
# docx → pdf rendering for the docx/pptx skills) *and* by liteparse
# (read_file's document parser) to convert docx/xlsx/pptx to PDF before
# parsing.  tesseract-ocr is the OCR engine: pytesseract wraps it for
# skills, and liteparse uses it as the OCR backend for scanned PDFs.
# imagemagick is used by liteparse to rasterise image inputs (tiff/
# webp/svg/etc.) before parsing.  pdfgrep is a nice CLI for full-text
# PDF search.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        curl \
        jq \
        tini \
        ca-certificates \
        gnupg \
        ffmpeg \
        poppler-utils \
        qpdf \
        pdftk-java \
        pandoc \
        tesseract-ocr \
        tesseract-ocr-eng \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        imagemagick \
        pdfgrep \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Global npm packages ─────────────────────────────────────────────
# pdf-lib and pdfjs-dist back the JavaScript snippets in the pdf skill;
# preinstalling them lets the skill examples run without `npm install`.
RUN npm install -g \
        docx \
        react-icons \
        react \
        react-dom \
        sharp \
        pptxgenjs \
        pdf-lib \
        pdfjs-dist \
        pnpm \
        @anthropic-ai/claude-code \
        @openai/codex \
    && npm cache clean --force

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

# PYTHONUSERBASE lives in the sandbox container's writable layer rather
# than on the s3fs mount.  s3fs serializes deleted-but-open files as
# `.fuse_hidden*` entries that briefly appear in directory listings, and
# uv (the backend for our pip wrapper) races them: it readdirs, sees a
# hidden entry, then stat()s it after FUSE has already dropped it,
# producing ENOENT and aborting the install.  Keeping site-packages off
# s3fs sidesteps the race entirely.  Tradeoff: pip-installed packages
# don't survive a sandbox-pod crash within a session (they're scoped to
# the pod's lifetime) — sessions are short-lived enough that a re-install
# on a fresh pod is cheap.
ENV PATH="/home/sandbox/.npm-global/bin:/home/sandbox/.local/bin:/opt/venv/bin:$PATH" \
    PYTHONUSERBASE="/home/sandbox/.local" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_USER=1 \
    NPM_CONFIG_PREFIX="/home/sandbox/.npm-global" \
    UV_CACHE_DIR="/home/sandbox/.cache/uv" \
    XDG_CACHE_HOME="/home/sandbox/.cache"

# ── Tool executor ───────────────────────────────────────────────────
COPY images/sandbox/tool-executor /usr/local/bin/tool-executor
RUN chmod +x /usr/local/bin/tool-executor

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home sandbox \
    && mkdir -p /workspace /home/sandbox/.local /home/sandbox/.npm-global /home/sandbox/.cache/uv \
    && chown -R sandbox:sandbox /workspace /home/sandbox/.local /home/sandbox/.npm-global /home/sandbox/.cache

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "surogates.sandbox.executor_server"]
