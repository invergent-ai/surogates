"""Built-in ``github`` tool — scoped GitHub REST API for the agent's repos.

The agent connects a GitHub PAT + a set of repositories in the Tools tab (the
same credential the coding tool uses to check out and open PRs).  This tool
lets the agent both *answer questions* about those repos — issues, pull
requests, files, commits, diffs, search — and *act* on them — open/close/comment
issues and PRs, add labels, write files — without a checkout, by proxying
authenticated requests to the GitHub REST API.

Scoped to the configured repos: a request must target ``/repos/<owner>/<repo>/…``
for a configured repo, or (read-only) ``/search/…`` qualified to a configured
repo.  ``GET`` is read-only; ``POST``/``PATCH``/``PUT``/``DELETE`` writes are
allowed on sub-resources of a configured repo but never on the repository
itself (no root-level delete/rename) or admin endpoints like ``transfer``.  A
write needs the connected PAT to carry the matching scope — the tool does not
widen the token.  The PAT rides in the ``Authorization`` header only — never in
argv, the returned data, or an event.

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

_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_METHODS = frozenset({"GET"}) | _WRITE_METHODS

# Repo sub-resources that reconfigure or hand off the whole repository — writes
# to these are refused even for a configured repo (they are not day-to-day
# issue/PR/content edits and are hard or impossible to undo).
_PROTECTED_REPO_SUBRESOURCES = frozenset({"transfer"})


def github_repo_slugs(repos: Sequence[Mapping[str, str]]) -> set[str]:
    """The ``owner/repo`` slugs (lowercased) for the configured repositories."""
    slugs: set[str] = set()
    for repo in repos or ():
        m = _SLUG_RE.search(repo.get("url", ""))
        if m:
            slugs.add(f"{m.group(1)}/{m.group(2)}".lower())
    return slugs


def authorize_github_request(
    method: str,
    path: str,
    params: Mapping[str, Any] | None,
    repos: Sequence[Mapping[str, str]],
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a GitHub REST call against *repos*.

    Allowed: ``/repos/<owner>/<repo>/…`` for a configured repo (any method), or
    ``/search/…`` (``GET`` only) whose ``q`` is scoped to configured repos via
    ``repo:``.  Writes (``POST``/``PATCH``/``PUT``/``DELETE``) are further
    refused on the repository itself and on protected admin sub-resources.
    """
    method = method.upper()
    is_write = method in _WRITE_METHODS

    slugs = github_repo_slugs(repos)
    if not slugs:
        return False, "No repository is configured for this agent (Tools tab)."

    path_only = "/" + path.split("?", 1)[0].strip().strip("/")
    query = dict(parse_qsl(path.partition("?")[2]))
    query.update({k: str(v) for k, v in (params or {}).items()})
    segs = path_only.strip("/").split("/")

    if segs[0] == "repos" and len(segs) >= 3:
        slug = f"{segs[1]}/{segs[2]}".lower()
        if slug not in slugs:
            return (
                False,
                f"'{slug}' is not a configured repository. Configured: "
                f"{', '.join(sorted(slugs))}.",
            )
        if is_write:
            if len(segs) == 3:
                return (
                    False,
                    f"Refusing a repository-level {method} on '{slug}': the github "
                    "tool will not delete or reconfigure the repo itself, only its "
                    "sub-resources (issues, pulls, comments, labels, contents, …).",
                )
            if segs[3].lower() in _PROTECTED_REPO_SUBRESOURCES:
                return (
                    False,
                    f"Refusing a {method} on '{slug}/{segs[3]}': this admin action "
                    "is blocked by the github tool.",
                )
        return True, ""

    if segs[0] == "search" and len(segs) >= 2:
        if is_write:
            return False, "Search is read-only; use GET."
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
        "GitHub REST API for the agent's configured repositories, WITHOUT "
        "checking out the repo. Read (GET, the default): issues, pull requests, "
        "files, commits, diffs, search. Write (set 'method' to POST/PATCH/PUT/"
        "DELETE with a 'body'): open/close/comment issues and PRs, add labels, "
        "write files. Scoped to configured repos; writes act only on the repo's "
        "sub-resources, never on the repository itself (no root delete/rename) "
        "and need the connected token to carry write scope. Read examples: list "
        "open issues → path='/repos/{owner}/{repo}/issues', params={'state':'open'}; "
        "a PR's diff → path='/repos/{owner}/{repo}/pulls/{n}', media_type='diff'; a "
        "file → path='/repos/{owner}/{repo}/contents/{filepath}'; search issues → "
        "path='/search/issues', params={'q':'repo:{owner}/{repo} is:open label:bug'}. "
        "Write examples: open an issue → method='POST', "
        "path='/repos/{owner}/{repo}/issues', body={'title':'…','body':'…'}; comment "
        "→ method='POST', path='/repos/{owner}/{repo}/issues/{n}/comments', "
        "body={'body':'…'}; close → method='PATCH', "
        "path='/repos/{owner}/{repo}/issues/{n}', body={'state':'closed'}. To make "
        "code changes across many files and open a PR, prefer run_coding_agent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "GitHub REST API path, e.g. /repos/{owner}/{repo}/issues",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"],
                "description": "HTTP method. Default 'GET' (read-only). Use POST/PATCH/"
                               "PUT/DELETE to write; pass the payload in 'body'.",
            },
            "params": {
                "type": "object",
                "description": "Query parameters (e.g. {'state':'open','per_page':20}).",
            },
            "body": {
                "type": "object",
                "description": "JSON request body for write methods (e.g. "
                               "{'title':'Bug','body':'…'} when creating an issue).",
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


async def _github_request(
    method: str,
    path: str,
    params: Mapping[str, Any],
    body: Mapping[str, Any] | None,
    pat: str,
    media_type: str,
) -> str:
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
    request_kwargs: dict[str, Any] = {"params": query, "headers": headers}
    if method != "GET" and body is not None:
        request_kwargs["json"] = body
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, f"{_API_BASE}/{path_only.strip('/')}", **request_kwargs,
            )
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"GitHub request failed: {exc}"})

    text = resp.text or ""
    truncated = len(text) > _MAX_BODY
    body_text = text[:_MAX_BODY]
    if resp.status_code >= 400:
        message = body_text
        try:
            message = (json.loads(text) or {}).get("message", body_text)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        return json.dumps({"status": resp.status_code, "error": message})

    data: Any = body_text or None
    if accept == _MEDIA_ACCEPT["json"] and text and not truncated:
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

    method = (arguments.get("method") or "GET").strip().upper()
    if method not in _METHODS:
        return json.dumps({
            "error": f"Unsupported method '{method}'. Use one of {sorted(_METHODS)}.",
        })

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
    ok, reason = authorize_github_request(method, path, params, repos)
    if not ok:
        return json.dumps({"error": reason, "code": "out_of_scope"})

    return await _github_request(
        method, path, params, arguments.get("body"), pat,
        arguments.get("media_type") or "json",
    )
