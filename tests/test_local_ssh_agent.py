"""Dev-only local ssh-agent lifecycle (process/docker backends)."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from surogates.sandbox.ssh_agent_local import (
    start_local_ssh_agent,
    stop_local_ssh_agent,
)

_HAS_AGENT = shutil.which("ssh-agent") is not None and shutil.which("ssh-keygen") is not None

pytestmark = pytest.mark.skipif(not _HAS_AGENT, reason="ssh tooling not installed")


async def test_local_agent_starts_and_socket_exists():
    sock = await start_local_ssh_agent("sid-empty", {})
    try:
        assert os.path.exists(sock)
    finally:
        await stop_local_ssh_agent("sid-empty")
    assert not os.path.exists(sock)


async def test_local_agent_is_idempotent():
    a = await start_local_ssh_agent("sid-idem", {})
    b = await start_local_ssh_agent("sid-idem", {})
    try:
        assert a == b
    finally:
        await stop_local_ssh_agent("sid-idem")


async def test_local_agent_loads_key_and_wipes_disk(tmp_path):
    # Generate a throwaway unencrypted ed25519 key.
    key_path = tmp_path / "k"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-q"],
        check=True,
    )
    private_key = key_path.read_text()

    sock = await start_local_ssh_agent(
        "sid-key", {"prod": {"private_key": private_key, "passphrase": ""}},
    )
    try:
        # The agent should list one identity.
        out = subprocess.run(
            ["ssh-add", "-l"],
            env={**os.environ, "SSH_AUTH_SOCK": sock},
            capture_output=True, text=True,
        )
        assert "ED25519" in out.stdout or "ssh-ed25519" in out.stdout
        # No private key file should linger in the agent dir.
        agent_dir = os.path.dirname(sock)
        assert not any(f.startswith("id_") for f in os.listdir(agent_dir))
    finally:
        await stop_local_ssh_agent("sid-key")
