"""Dev-only local ssh-agent for the process/docker sandbox backends.

**Non-production isolation.**  Unlike the k8s backend — which runs the ssh-agent
in a separate hardened container the untrusted sandbox cannot read — this runs an
``ssh-agent`` process on the worker host and exposes its socket to the local
sandbox.  It exists solely so the SSH remote-access feature is testable on the
``process``/``docker`` backends during development.  It MUST NOT be used in
production; the k8s backend is the only isolated delivery path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile

logger = logging.getLogger(__name__)

# sandbox_id -> (agent_pid, agent_dir)
_AGENTS: dict[str, tuple[int, str]] = {}


async def _run(*args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **(env or {})},
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def start_local_ssh_agent(
    sandbox_id: str, ssh_key_material: dict[str, dict[str, str]],
) -> str:
    """Start a local ssh-agent, load the keys, and return the socket path.

    Idempotent per ``sandbox_id`` — a second call returns the existing socket.
    """
    if sandbox_id in _AGENTS:
        _pid, agent_dir = _AGENTS[sandbox_id]
        return os.path.join(agent_dir, "auth.sock")

    agent_dir = tempfile.mkdtemp(prefix=f"sshagent-{sandbox_id[:8]}-")
    os.chmod(agent_dir, stat.S_IRWXU)
    sock = os.path.join(agent_dir, "auth.sock")

    rc, out = await _run("ssh-agent", "-a", sock)
    if rc != 0:
        shutil.rmtree(agent_dir, ignore_errors=True)
        raise RuntimeError(f"ssh-agent failed to start: {out}")
    pid = 0
    for line in out.splitlines():
        if line.startswith("SSH_AGENT_PID="):
            pid = int(line.split("=", 1)[1].split(";", 1)[0])
            break
    _AGENTS[sandbox_id] = (pid, agent_dir)

    for name, mat in ssh_key_material.items():
        await _add_key(sock, agent_dir, name, mat)
    return sock


async def _add_key(
    sock: str, agent_dir: str, name: str, mat: dict[str, str],
) -> None:
    key_path = os.path.join(agent_dir, f"id_{name}")
    with open(key_path, "w", encoding="utf-8") as fh:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        fh.write(mat.get("private_key", ""))
    try:
        passphrase = mat.get("passphrase") or ""
        if passphrase:
            askpass = os.path.join(agent_dir, f"askpass_{name}")
            pass_file = os.path.join(agent_dir, f"pass_{name}")
            with open(pass_file, "w", encoding="utf-8") as fh:
                os.chmod(pass_file, stat.S_IRUSR | stat.S_IWUSR)
                fh.write(passphrase)
            with open(askpass, "w", encoding="utf-8") as fh:
                fh.write(f'#!/bin/sh\ncat {pass_file}\n')
            os.chmod(askpass, stat.S_IRWXU)
            rc, out = await _run(
                "ssh-add", key_path,
                env={
                    "SSH_AUTH_SOCK": sock,
                    "SSH_ASKPASS": askpass,
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": ":0",
                },
            )
            os.unlink(pass_file)
            os.unlink(askpass)
        else:
            rc, out = await _run("ssh-add", key_path, env={"SSH_AUTH_SOCK": sock})
        if rc != 0:
            logger.warning("ssh-add failed for key %s: %s", name, out.strip())
    finally:
        # The key must never linger on disk beyond the load.
        if os.path.exists(key_path):
            os.unlink(key_path)


async def stop_local_ssh_agent(sandbox_id: str) -> None:
    """Kill the local ssh-agent and remove its socket dir."""
    entry = _AGENTS.pop(sandbox_id, None)
    if entry is None:
        return
    pid, agent_dir = entry
    if pid:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
    shutil.rmtree(agent_dir, ignore_errors=True)
