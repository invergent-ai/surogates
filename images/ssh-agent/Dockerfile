# Isolated ssh-agent sidecar for Surogates sandbox pods.
#
# Holds an agent's SSH private key(s) in a separate container the untrusted
# sandbox cannot read or ptrace. The main sandbox container is handed ONLY the
# auth socket (a shared in-memory volume), so it can authenticate to the
# configured remote hosts but can never read or exfiltrate the key material.
#
# The sidecar is launched with an inline `/bin/sh -c` script (see
# surogates/sandbox/kubernetes.py::_build_ssh_sidecar) that runs `ssh-agent`,
# loads each key with `ssh-add`, then idles holding the socket. It runs as a
# non-root uid with a read-only root filesystem and all Linux capabilities
# dropped; the key Secret and socket arrive via mounted volumes, so the image
# only needs the OpenSSH client tooling and a POSIX shell.
#
# Image name (surogates-ssh-agent) matches the surogates config default
# `sandbox.k8s_ssh_agent_image`.
#
# Build:
#   docker build -t ghcr.io/invergent-ai/surogates-ssh-agent:latest -f images/ssh-agent/Dockerfile .

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# The pod securityContext pins runAsUser=65532 (nonroot) with a read-only root
# filesystem; declaring it here keeps the image safe to run standalone too.
USER 65532:65532
