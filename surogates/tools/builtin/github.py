"""Built-in ``github`` tool — read-only GitHub REST API for the agent's repos.

The agent connects a GitHub PAT + a set of repositories in the Tools tab (the
same credential the coding tool uses to check out and open PRs).  This tool
lets the agent *answer questions* about those repos — issues, pull requests,
files, commits, diffs, search — without a checkout, by proxying authenticated
``GET`` requests to the GitHub REST API.

Read-only (``GET`` only) and scoped to the configured repos: a request must
target ``/repos/<owner>/<repo>/…`` for a configured repo, or ``/search/…``
qualified to a configured repo.  The PAT rides in the ``Authorization`` header
only — never in argv, the returned data, or an event.

Required kwargs (injected by the harness dispatch): ``tenant``,
``credential_vault``, ``session_config`` (carrying the agent's ``repos``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl

from surogates.coding_agents.repo_resolve import resolve_git_pat
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_MAX_BODY = 30_000  # cap the returned payload; note truncation

# Accept header per requested media type — lets the agent pull diffs/patches
# and raw file contents, not just JSON.
_MEDIA_ACCEPT = {
    "json": "application/vnd.github+json",
    "diff": "application/vnd.github.diff",
    "patch": "application/vnd.github.patch",
    "raw": "application/vnd.github.raw",
}

_SLUG_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE)


def github_repo_slugs(repos: Sequence[Mapping[str, str]]) -> set[str]:
    """The ``owner/repo`` slugs (lowercased) for the configured repositories."""
    slugs: set[str] = set()
    for repo in repos or ():
        m = _SLUG_RE.search(repo.get("url", ""))
        if m:
            slugs.add(f"{m.group(1)}/{m.group(2)}".lower())
    return slugs


def authorize_github_read(
    path: str, params: Mapping[str, Any] | None, repos: Sequence[Mapping[str, str]],
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a GitHub REST path against *repos*.

    Allowed: ``/repos/<owner>/<repo>/…`` for a configured repo, or
    ``/search/…`` whose ``q`` is scoped to configured repos via ``repo:``.
    """
    slugs = github_repo_slugs(repos)
    if not slugs:
        return False, "No repository is configured for this agent (Tools tab)."

    path_only = "/" + path.split("?", 1)[0].strip().strip("/")
    query = dict(parse_qsl(path.partition("?")[2]))
    query.update({k: str(v) for k, v in (params or {}).items()})
    segs = path_only.strip("/").split("/")

    if segs[0] == "repos" and len(segs) >= 3:
        slug = f"{segs[1]}/{segs[2]}".lower()
        if slug in slugs:
            return True, ""
        return (
            False,
            f"'{slug}' is not a configured repository. Configured: "
            f"{', '.join(sorted(slugs))}.",
        )

    if segs[0] == "search" and len(segs) >= 2:
        q = query.get("q", "")
        if re.search(r"\b(?:org|user):", q, re.IGNORECASE):
            return False, "Search must target a configured repo (repo:<owner>/<repo>), not org:/user:."
        repo_quals = re.findall(r"repo:(\S+)", q, re.IGNORECASE)
        if not repo_quals:
            return False, "Search must include a repo:<owner>/<repo> qualifier for a configured repo."
        for rq in repo_quals:
            if rq.lower() not in slugs:
                return False, f"Search repo:{rq} is not a configured repository."
        return True, ""

    return (
        False,
        "The github tool is scoped to configured repos: use /repos/<owner>/<repo>/… "
        "or /search with a repo:<owner>/<repo> qualifier.",
    )


_SCHEMA = ToolSchema(
    name="github",
    description=(
        "Read-only GitHub REST API for the agent's configured repositories — "
        "answer questions about issues, pull requests, files, commits, diffs, "
        "and search WITHOUT checking out the repo. Give the REST 'path' (and "
        "optional 'params'); scoped to configured repos, GET-only. Examples: "
        "list open issues → path='/repos/{owner}/{repo}/issues', "
        "params={'state':'open'}; a PR's diff → path='/repos/{owner}/{repo}/pulls/{n}', "
        "media_type='diff'; a file → path='/repos/{owner}/{repo}/contents/{filepath}'; "
        "recent commits → path='/repos/{owner}/{repo}/commits'; search issues → "
        "path='/search/issues', params={'q':'repo:{owner}/{repo} is:open label:bug'}. "
        "To make code changes and open a PR, use run_coding_agent instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "GitHub REST API path, e.g. /repos/{owner}/{repo}/issues",
            },
            "params": {
                "type": "object",
                "description": "Query parameters (e.g. {'state':'open','per_page':20}).",
            },
            "media_type": {
                "type": "string",
                "enum": ["json", "diff", "patch", "raw"],
                "description": "Response format; 'diff'/'patch' for PR/commit diffs, "
                               "'raw' for file contents. Default 'json'.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``github`` tool."""
    registry.register(
        name="github",
        schema=_SCHEMA,
        handler=_github_handler,
        toolset="code",
    )


async def _github_get(path: str, params: Mapping[str, Any], pat: str, media_type: str) -> str:
    import httpx

    path_only, _, qs = path.partition("?")
    query = dict(parse_qsl(qs))
    query.update({k: str(v) for k, v in (params or {}).items()})
    accept = _MEDIA_ACCEPT.get(media_type, _MEDIA_ACCEPT["json"])
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "surogate-agent",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_API_BASE}/{path_only.strip('/')}", params=query, headers=headers,
            )
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"GitHub request failed: {exc}"})

    text = resp.text or ""
    truncated = len(text) > _MAX_BODY
    body = text[:_MAX_BODY]
    if resp.status_code >= 400:
        message = body
        try:
            message = (json.loads(text) or {}).get("message", body)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        return json.dumps({"status": resp.status_code, "error": message})

    data: Any = body
    if accept == _MEDIA_ACCEPT["json"] and not truncated:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            pass
    result: dict[str, Any] = {"status": resp.status_code, "data": data}
    if truncated:
        result["truncated"] = True
        result["note"] = f"Response truncated to {_MAX_BODY} chars; narrow the query or paginate."
    return json.dumps(result)


async def _github_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    path = (arguments.get("path") or "").strip()
    if not path:
        return json.dumps({"error": "path is required, e.g. /repos/{owner}/{repo}/issues"})

    tenant = kwargs.get("tenant")
    vault = kwargs.get("credential_vault")
    if tenant is None or vault is None:
        return json.dumps({"error": "the github tool is not available on this deployment"})

    effective_config = kwargs.get("session_config") or {}
    repos = effective_config.get("repos") or []
    if not repos:
        return json.dumps({
            "error": "Configure a repository in the agent's Tools tab first.",
            "code": "repo_not_configured",
        })

    pat = await resolve_git_pat(
        vault, org_id=tenant.org_id,
        user_id=getattr(tenant, "user_id", None),
        service_account_id=getattr(tenant, "service_account_id", None),
    )
    if not pat:
        return json.dumps({
            "error": "Connect a GitHub token in the agent's Tools tab first.",
            "code": "git_pat_not_connected",
        })

    params = arguments.get("params") or {}
    ok, reason = authorize_github_read(path, params, repos)
    if not ok:
        return json.dumps({"error": reason, "code": "out_of_scope"})

    return await _github_get(path, params, pat, arguments.get("media_type") or "json")
