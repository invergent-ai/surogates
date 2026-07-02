"""Resolve which repo a coding run targets + the git PAT for it.

Repo selection is pure; PAT resolution reads ``vault://git_pat`` under exactly
one credential principal (the agent service account when present, else the
acting user) — no org fallback, so one agent's token never resolves for
another.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _repo_name(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()


def select_repo(
    repos: Sequence[Mapping[str, str]], requested: str | None,
) -> dict | None:
    """Pick a repo by name/url match, else the sole repo, else None.

    Returns a copied plain dict so callers can pass it downstream without
    mutating the runtime context it came from.
    """
    if not repos:
        return None
    if requested:
        r = _repo_name(requested)
        for repo in repos:
            url = repo.get("url", "")
            if r == _repo_name(url) or requested.rstrip("/") == url.rstrip("/"):
                return dict(repo)
        return None
    return dict(repos[0]) if len(repos) == 1 else None


def principal_kwargs(*, user_id=None, service_account_id=None) -> dict[str, Any]:
    """Credential-principal kwargs: the agent SA wins, else the acting user."""
    if service_account_id is not None:
        return {"user_id": None, "service_account_id": service_account_id}
    return {"user_id": user_id, "service_account_id": None}


async def resolve_git_pat(
    vault: Any, *, org_id, user_id=None, service_account_id=None,
) -> str | None:
    return await vault.resolve_ref(
        "vault://git_pat",
        org_id=org_id,
        **principal_kwargs(user_id=user_id, service_account_id=service_account_id),
    )
