"""Resolve vaulted SSH keys and render per-agent SSH target config.

Mirrors :mod:`surogates.coding_agents.repo_resolve`: principal-scoped vault
resolution plus a system-prompt section.  The private key is resolved
worker-side; only the generated ``ssh_config``/``known_hosts`` (non-secret) ever
reach the sandbox.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from surogates.ssh_access.bundle import SSHKeyBundle

SSH_KEY_PREFIX = "ssh_key:"

# Defense-in-depth against ``~/.ssh/config`` injection (a newline in
# ``alias``/``host``/``user`` would forge extra config directives) and
# sidecar-script injection (a metachar in ``key_name`` reaches a shell).
# ``alias``/``user``/``key_name`` are conservative identifiers; ``host`` also
# allows ``:`` for IPv6 literals — but neither permits whitespace/newlines.
_IDENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def ssh_key_ref(name: str) -> str:
    """The ``vault://`` reference for a named SSH key."""
    return f"vault://{SSH_KEY_PREFIX}{name}"


def _principal_kwargs(*, user_id=None, service_account_id=None) -> dict[str, Any]:
    """Credential-principal kwargs: the agent SA wins, else the acting user."""
    if service_account_id is not None:
        return {"user_id": None, "service_account_id": service_account_id}
    return {"user_id": user_id, "service_account_id": None}


async def resolve_ssh_key(
    vault: Any, *, org_id, name: str, user_id=None, service_account_id=None,
) -> SSHKeyBundle | None:
    """Resolve ``ssh_key:<name>`` under the caller's principal, or ``None``."""
    raw = await vault.resolve_ref(
        ssh_key_ref(name),
        org_id=org_id,
        **_principal_kwargs(user_id=user_id, service_account_id=service_account_id),
    )
    return SSHKeyBundle.from_json(raw) if raw else None


def validate_targets(raw: Sequence[Mapping[str, Any]] | None) -> list[dict]:
    """Normalize + validate ssh targets; raise ``ValueError`` on bad input.

    Each output entry is ``{alias, host, port, user, key_name, host_key}`` with
    ``user``/``host_key`` possibly ``None``.  Aliases must be unique.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for t in raw or ():
        alias = str(t.get("alias", "")).strip()
        host = str(t.get("host", "")).strip()
        key_name = str(t.get("key_name", "")).strip()
        if not alias or not host or not key_name:
            raise ValueError("each ssh target needs alias, host, and key_name")
        if not _IDENT_RE.match(alias):
            raise ValueError(f"invalid ssh target alias: {alias!r}")
        if not _HOST_RE.match(host):
            raise ValueError(f"invalid ssh target host: {host!r}")
        if not _IDENT_RE.match(key_name):
            raise ValueError(f"invalid ssh target key_name: {key_name!r}")
        user = str(t.get("user", "")).strip()
        if user and not _IDENT_RE.match(user):
            raise ValueError(f"invalid ssh target user: {user!r}")
        if alias in seen:
            raise ValueError(f"duplicate ssh target alias: {alias}")
        seen.add(alias)
        raw_port = t.get("port")
        port = 22 if raw_port is None else int(raw_port)
        if not (1 <= port <= 65535):
            raise ValueError(f"invalid port for {alias}: {port}")
        out.append({
            "alias": alias,
            "host": host,
            "port": port,
            "user": user or None,
            "key_name": key_name,
            "host_key": (str(t.get("host_key")).strip() if t.get("host_key") else None),
        })
    return out


def build_ssh_config(
    targets: Sequence[Mapping[str, Any]],
    known_hosts_path: str = "~/.ssh/known_hosts",
) -> str:
    """Render ``ssh_config`` from targets (no secret; safe for the sandbox).

    Each host verifies against the pinned ``known_hosts``
    (``StrictHostKeyChecking yes`` + ``UserKnownHostsFile <known_hosts_path>``).
    Authentication uses the agent's key via ``SSH_AUTH_SOCK`` (ssh consults the
    agent socket by default) — we deliberately emit neither ``IdentityAgent``
    (the ``${SSH_AUTH_SOCK}`` form isn't reliably expanded in ssh_config) nor
    ``IdentitiesOnly yes`` (which, with no ``IdentityFile``, would suppress the
    agent's keys and break publickey auth).
    """
    blocks: list[str] = []
    for t in targets or ():
        lines = [
            f"Host {t['alias']}",
            f"    HostName {t['host']}",
            f"    Port {int(t.get('port') or 22)}",
        ]
        if t.get("user"):
            lines.append(f"    User {t['user']}")
        lines.extend([
            "    StrictHostKeyChecking yes",
            f"    UserKnownHostsFile {known_hosts_path}",
        ])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def build_known_hosts(targets: Sequence[Mapping[str, Any]]) -> str:
    """Render ``~/.ssh/known_hosts`` from pinned ``host_key`` lines only."""
    lines = [str(t["host_key"]).strip() for t in (targets or ()) if t.get("host_key")]
    return "\n".join(lines) + ("\n" if lines else "")


def ssh_key_names(targets: Sequence[Mapping[str, Any]]) -> list[str]:
    """Distinct ``key_name``s referenced by the targets, in first-seen order."""
    out: list[str] = []
    for t in targets or ():
        name = str(t.get("key_name", "")).strip()
        if name and name not in out:
            out.append(name)
    return out


def write_ssh_home(
    home: str, targets: Sequence[Mapping[str, Any]], known_hosts: str,
) -> None:
    """Write ``<home>/.ssh/config`` + ``known_hosts`` (no secrets) for the terminal.

    Called just before an SSH-enabled command runs, under the child's ``HOME``.
    Rewriting on every invocation re-pins ``known_hosts`` and restores the
    strict config, so the agent cannot persistently weaken host-key checking by
    editing the files between commands.  Both files are non-secret (the private
    key lives in the isolated ssh-agent), so writing them into the workspace
    home is safe.
    """
    if not targets:
        return
    ssh_dir = Path(home) / ".ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ssh_dir, 0o700)
    kh = ssh_dir / "known_hosts"
    kh.write_text(known_hosts or "", encoding="utf-8")
    os.chmod(kh, 0o600)
    cfg = ssh_dir / "config"
    cfg.write_text(build_ssh_config(targets, known_hosts_path=str(kh)), encoding="utf-8")
    os.chmod(cfg, 0o600)


def render_ssh_targets_prompt(targets: Sequence[Mapping[str, Any]] | None) -> str:
    """A system-prompt section naming the agent's SSH targets.

    Empty when no valid target is configured, so the section only appears for
    agents that actually have SSH access.  The inverse of the git guardrail: it
    tells the agent the terminal *is* authenticated for these hosts.
    """
    lines = []
    for t in targets or ():
        alias = str(t.get("alias", "")).strip()
        host = str(t.get("host", "")).strip()
        if not alias or not host:
            continue
        user = str(t.get("user", "")).strip()
        target = f"{user}@{host}" if user else host
        lines.append(f"- `{alias}` → {target}")
    if not lines:
        return ""
    return (
        "## SSH remote hosts\n"
        "Your terminal is SSH-authenticated for these hosts via ssh-agent — "
        "use `ssh <alias> …` (do NOT specify a key file):\n"
        + "\n".join(lines)
        + "\n\nThe private key is held by an isolated agent; you can authenticate "
        "but cannot read or copy the key. Connections to hosts not listed here "
        "are blocked by network policy."
    )
