# Surogates Sandbox — agent execution environment, and Surogate Desktop's VM guest
#
# One shared `tools` stage holds what commands find: the OS packages, Node and
# the global npm packages, the Python venv with the data libraries, uv and the
# pip wrapper. Two images build on it, so `which` answers alike in both:
#
#   sandbox  the cloud sandbox: the surogates package (so the real tool handlers
#            run inside the sandbox with full feature parity), the tool
#            executor, geesefs and the coding agents. The last stage, so a
#            build without --target builds it.
#   guest    the desktop VM's root filesystem: no geesefs, no claude-code or
#            codex (switched off for local folders); the stub init, the pinned
#            kernel's modules, the guest-kernel rule and bpftool, iproute2,
#            e2fsprogs and kmod.
#
# Build (from repo root):
#   docker build -t ghcr.io/invergent-ai/surogates-agent-sandbox:latest -f images/sandbox/Dockerfile .
#   images/guest/build.sh <out-dir>     # the guest's rootfs.img and vmlinuz
# Load into k3d cluster (optional, if using k3d):
#   k3d image import ghcr.io/invergent-ai/surogates-agent-sandbox:latest -c surogates

# The guest's kernel: Ubuntu 24.04's generic kernel, booted directly. Its .debs
# come from Launchpad, which keeps every published version, and are checked by hash.
ARG KERNEL=6.8.0-146-generic

# ── Python dependencies: the data libraries ──────────────────────────
FROM python:3.12-slim AS python-deps

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv venv /opt/venv \
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
        pdf2image \
        pypdf \
        python-docx \
        python-pptx \
        openpyxl
# The extra libraries above (reportlab, pdfplumber, pypdfium2,
# pytesseract, pdf2image, pypdf, python-docx, python-pptx, openpyxl) are
# referenced by the bundled productivity skills (pdf/docx/xlsx/pptx);
# preinstalling them lets the skills run without any `pip install` step,
# in the cloud and in the desktop's guest.  Pandoc the binary comes from
# the apt layer below (the `pandoc` PyPI package is just a wrapper).

# ── Shared tools ─────────────────────────────────────────────────────
FROM python:3.12-slim AS tools

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
        git openssh-client \
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
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    # gh (GitHub CLI) lets a coding-agent run commit, push, and open a PR on
    # a checked-out repo; git is already installed in the base apt list above.
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
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
# The guest gives each root the same layout under its own HOME: the guest
# stage's /etc/surogate/environment names these variables, so a variable
# added here is added to its list too.
ENV PATH="/home/sandbox/.npm-global/bin:/home/sandbox/.local/bin:/opt/venv/bin:$PATH" \
    PYTHONUSERBASE="/home/sandbox/.local" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_USER=1 \
    NPM_CONFIG_PREFIX="/home/sandbox/.npm-global" \
    UV_CACHE_DIR="/home/sandbox/.cache/uv" \
    XDG_CACHE_HOME="/home/sandbox/.cache"

# ── The guest's kernel ──────────────────────────────────────────────
# The kernel pin: KERNEL above and these two .debs, their version and hashes, change together.
FROM python:3.12-slim AS kernel
ADD --checksum=sha256:5ff8d86009ba6c30a6aacfe47a5176969bbb6591438c913d29881032d071e83f \
    https://launchpad.net/ubuntu/+archive/primary/+files/linux-image-6.8.0-146-generic_6.8.0-146.146_amd64.deb /tmp/image.deb
ADD --checksum=sha256:9287a0b722a7dd0c94a412eda8e27de4742a7925fdf2d08375b2f608516e6ab4 \
    https://launchpad.net/ubuntu/+archive/primary/+files/linux-modules-6.8.0-146-generic_6.8.0-146.146_amd64.deb /tmp/modules.deb
RUN dpkg-deb -x /tmp/image.deb /kernel && dpkg-deb -x /tmp/modules.deb /kernel

# ── The guest-kernel rule (BPF-LSM), compiled against the pinned kernel's BTF ──
# The BTF is the pinned vmlinuz's own: its zstd payload is a vmlinux that keeps .BTF.
FROM ubuntu:24.04 AS bpf
ARG KERNEL
RUN apt-get update && apt-get install -y --no-install-recommends clang llvm libbpf-dev linux-tools-${KERNEL} zstd \
    && rm -rf /var/lib/apt/lists/*
COPY --from=kernel /kernel/boot/vmlinuz-${KERNEL} /vmlinuz
COPY desktop/vm/rule.bpf.c desktop/vm/rule-match.h /src/
# zstd stops with an error at the bytes after the payload; bpftool refuses anything but a whole vmlinux.
# Each lsm/<hook> program must take as many arguments as bpf_lsm_<hook> in the BTF (its FUNC_PROTO's vlen):
# the verifier loads one that does not, which then reads the wrong ones, or the hook's return slot as one.
# The check counts the commas of the BPF_PROG( line after each SEC, so it misjudges a declaration split
# across lines, or one that also takes the optional trailing int ret: keep each on one line, without ret.
RUN at=$(LC_ALL=C grep -aboP '\x28\xb5\x2f\xfd' /vmlinuz | head -1 | cut -d: -f1) \
    && { tail -c +$((at + 1)) /vmlinuz | zstd -dcq > /vmlinux || true; } \
    && { test -s /vmlinux || { echo "no vmlinux decompressed from /vmlinuz" >&2; exit 1; }; } \
    && /usr/lib/linux-tools/${KERNEL}/bpftool btf dump file /vmlinux format c > /src/vmlinux.h \
    && /usr/lib/linux-tools/${KERNEL}/bpftool btf dump file /vmlinux format raw | awk ' \
        FNR == NR && $2 == "FUNC_PROTO" { for (i = 3; i <= NF; i++) if ($i ~ /^vlen=/) vlen[substr($1, 2, length($1) - 2)] = substr($i, 6); next; } \
        FNR == NR && $2 == "FUNC" && $3 ~ /^.bpf_lsm_/ { proto[substr($3, 10, length($3) - 10)] = substr($4, 9); next; } \
        FNR == NR { next; } \
        hook != "" && !/BPF_PROG\(/ { print "lsm/" hook ": no BPF_PROG on the line after its SEC" > "/dev/stderr"; bad = 1; hook = ""; } \
        hook != "" { args = $0; sub(/.*BPF_PROG\(/, "", args); sub(/\).*/, "", args); n = gsub(/,/, "", args); want = vlen[proto[hook]]; \
          if (want == "") { print "lsm/" hook ": the pinned kernel has no bpf_lsm_" hook > "/dev/stderr"; bad = 1; } \
          else if (n != want) { print "lsm/" hook ": its program takes " n " arguments, and bpf_lsm_" hook " in the pinned kernel takes " want > "/dev/stderr"; bad = 1; } \
          checked++; hook = ""; } \
        /^SEC\("lsm\// { hook = $0; sub(/^SEC\("lsm\//, "", hook); sub(/".*/, "", hook); } \
        END { if (bad || !checked) exit 1; print checked " lsm programs take as many arguments as their hooks"; }' \
        - /src/rule.bpf.c \
    && clang -O2 -g -target bpf -D__TARGET_ARCH_x86 -I/src -c /src/rule.bpf.c -o /rule.bpf.o

# ── Surogate Desktop's VM guest ─────────────────────────────────────
FROM tools AS guest
ARG KERNEL
# libelf1, zlib1g, libcap2 and libzstd1 are bpftool's libraries.
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 e2fsprogs kmod \
        libelf1 zlib1g libcap2 libzstd1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=kernel /kernel/lib/modules/ /usr/lib/modules/
COPY --from=bpf /rule.bpf.o /usr/lib/surogate/rule.bpf.o
COPY --from=bpf --chmod=0755 /usr/lib/linux-tools/${KERNEL}/bpftool /usr/sbin/bpftool
COPY --chmod=0755 images/guest/surogate-init /usr/sbin/surogate-init
# No setuid programs and no file capabilities: the image is mounted read-only,
# so none comes back. /etc/surogate/environment is the commands' environment,
# the cloud's layout, which the guest agent moves under each root's own HOME.
RUN depmod "$KERNEL" \
    && find / -xdev -type f -perm /6000 -exec chmod ug-s {} + \
    && find / -xdev -type f -exec python3 -c 'import os, sys; [os.removexattr(p, "security.capability") for p in sys.argv[1:] if "security.capability" in os.listxattr(p)]' {} + \
    && mkdir -p /etc/surogate \
    && printenv | grep -E '^(PATH|PYTHONUSERBASE|PYTHONUNBUFFERED|PYTHONDONTWRITEBYTECODE|PIP_USER|NPM_CONFIG_PREFIX|UV_CACHE_DIR|XDG_CACHE_HOME)=' \
        > /etc/surogate/environment
# Docker writes these two at run time, so neither is in a layer. localhost
# resolves without DNS, and the guest has no DNS: the host resolves the names
# in each connection's request.
COPY <<EOF /etc/hosts
127.0.0.1 localhost
::1 localhost ip6-localhost ip6-loopback
EOF
COPY <<EOF /etc/resolv.conf
# No DNS in the guest: the host resolves the names in each connection's request.
EOF

# The guest's disks, made in the build so the tree keeps its owners and modes:
# docker build --target guest-disk --output type=local,dest=<dir> (images/guest/build.sh).
FROM python:3.12-slim AS guest-disk-build
ARG KERNEL
RUN apt-get update && apt-get install -y --no-install-recommends e2fsprogs zstd \
    && rm -rf /var/lib/apt/lists/*
# Sized from the tree: 10% and 64 MiB over its blocks, an inode for every entry and a fifth more.
RUN --mount=type=bind,from=guest,target=/tree \
    --mount=type=bind,from=kernel,source=/kernel/boot,target=/boot \
    mkdir /out \
    && blocks=$(du -sxm /tree | cut -f1) \
    && inodes=$(find /tree -xdev | wc -l) \
    && mke2fs -q -t ext4 -O ^has_journal -L surogate-guest -N $((inodes * 6 / 5)) \
        -d /tree /out/rootfs.img "$((blocks * 11 / 10 + 64))M" \
    && zstd -q -T0 -19 --rm /out/rootfs.img \
    && cp "/boot/vmlinuz-$KERNEL" /out/vmlinuz && chmod 0644 /out/vmlinuz

FROM scratch AS guest-disk
COPY --from=guest-disk-build /out/ /

# ── The cloud sandbox ───────────────────────────────────────────────
FROM tools AS sandbox

LABEL org.opencontainers.image.title="surogates-agent-sandbox" \
      org.opencontainers.image.description="Surogates agent sandbox execution environment" \
      org.opencontainers.image.vendor="Invergent SA" \
      org.opencontainers.image.source="https://github.com/invergent-ai/surogates"

# The coding agents, beside the shared stage's npm packages: in /usr, not the
# NPM_CONFIG_PREFIX the shared stage sets for the agent's own installs. fuse is
# for geesefs below.
RUN apt-get update && apt-get install -y --no-install-recommends fuse \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --prefix /usr @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force

# The surogates package, so the real tool handlers run inside the sandbox.
# liteparse comes with it (project deps).
COPY pyproject.toml /build/pyproject.toml
COPY surogates/ /build/surogates/
RUN uv pip install --python /opt/venv/bin/python --no-cache /build \
    && rm -rf /build

# ── Tool executor ───────────────────────────────────────────────────
COPY images/sandbox/tool-executor /usr/local/bin/tool-executor
RUN chmod +x /usr/local/bin/tool-executor

# ── geesefs (inline s3fs mount for docker-mode sandboxes) ───────────
# Same pinned build as the s3fs sidecar image. Inert in K8s/prod: the
# inline mount is gated behind SANDBOX_S3FS_INLINE, and the K8s manifest
# overrides the container command entirely. user_allow_other lets the
# non-root sandbox user mount with -o allow_other.
ARG GEESEFS_VERSION=v0.43.7
RUN echo "user_allow_other" >> /etc/fuse.conf \
    && curl -fsSL -o /usr/local/bin/geesefs \
        "https://github.com/yandex-cloud/geesefs/releases/download/${GEESEFS_VERSION}/geesefs-linux-amd64" \
    && chmod +x /usr/local/bin/geesefs

# ── Non-root user ───────────────────────────────────────────────────
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid sandbox --create-home sandbox \
    && mkdir -p /workspace /home/sandbox/.local /home/sandbox/.npm-global /home/sandbox/.cache/uv \
    && chown -R sandbox:sandbox /workspace /home/sandbox/.local /home/sandbox/.npm-global /home/sandbox/.cache

COPY images/sandbox/entrypoint.sh /usr/local/bin/sandbox-entrypoint
RUN chmod +x /usr/local/bin/sandbox-entrypoint

USER sandbox
WORKDIR /workspace

ENTRYPOINT ["tini", "--"]
CMD ["/usr/local/bin/sandbox-entrypoint"]
