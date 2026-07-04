"""Resolve vaulted SSH keys and render per-agent SSH target config.

Mirrors :mod:`surogates.coding_agents.repo_resolve`: principal-scoped vault
resolution plus a system-prompt section.  The private key is resolved
worker-side; only the generated ``ssh_config``/``known_hosts`` (non-secret) ever
reach the sandbox.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from surogates.ssh_access.bundle import SSHKeyBundle

SSH_KEY_PREFIX = "ssh_key:"


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
            "user": str(t.get("user", "")).strip() or None,
            "key_name": key_name,
            "host_key": (str(t.get("host_key")).strip() if t.get("host_key") else None),
        })
    return out


def build_ssh_config(targets: Sequence[Mapping[str, Any]]) -> str:
    """Render ``~/.ssh/config`` from targets (no secret; safe for the sandbox).

    Every host authenticates only through the agent socket (``IdentityAgent``
    + ``IdentitiesOnly``) and verifies against the pinned ``known_hosts``
    (``StrictHostKeyChecking yes``).
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
            "    IdentityAgent ${SSH_AUTH_SOCK}",
            "    IdentitiesOnly yes",
            "    StrictHostKeyChecking yes",
            "    UserKnownHostsFile ~/.ssh/known_hosts",
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
