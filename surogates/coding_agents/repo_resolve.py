"""Resolve which repo a coding run targets + the git PAT for it.

Repo selection is pure; PAT resolution reads ``vault://git_pat`` under exactly
one credential principal (the agent service account when present, else the
acting user) — no org fallback, so one agent's token never resolves for
another.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SLUG_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


def _repo_name(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()


def _repo_slug(url: str) -> str | None:
    """``owner/repo`` for a GitHub URL, or None if it isn't one."""
    m = _SLUG_RE.search(url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def render_repos_prompt(repos: Sequence[Mapping[str, str]] | None) -> str:
    """A system-prompt section naming the agent's configured GitHub repos.

    Empty when no valid repo is configured, so the section only appears for
    agents that actually have repositories.  Tells the agent which repos it can
    act on (so it doesn't guess names) and to link the artifacts it mentions.
    """
    lines: list[str] = []
    for repo in repos or ():
        slug = _repo_slug(repo.get("url", ""))
        if not slug:
            continue
        branch = repo.get("default_branch", "")
        lines.append(f"- {slug}" + (f" (default branch: {branch})" if branch else ""))
    if not lines:
        return ""
    return (
        "## GitHub repositories\n"
        "You have access to these GitHub repositories:\n"
        + "\n".join(lines)
        + "\n\nUse the `github` tool to read them (issues, pull requests, files, "
        "commits, diffs, search) and `run_coding_agent` or `/code` to make changes "
        "and open a pull request. When you reference an issue, commit, or pull "
        "request, include its GitHub link — e.g. "
        "`https://github.com/<owner>/<repo>/issues/<number>`, "
        "`.../pull/<number>`, or `.../commit/<sha>`."
    )


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
