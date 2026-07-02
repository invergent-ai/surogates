"""The github tool proxies authenticated, scoped, read-only GitHub API calls."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import respx

from surogates.tools.builtin.github import _github_handler

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PAT = "github_pat_11ABCDEsecret9876"
_REPO = {"url": "https://github.com/acme/api", "default_branch": "main"}


class _FakeVault:
    def __init__(self, pat=None):
        self.pat = pat

    async def resolve_ref(self, ref, *, org_id, user_id=None, service_account_id=None):
        return self.pat if ref == "vault://git_pat" else None


def _tenant():
    return SimpleNamespace(org_id=uuid4(), user_id=uuid4(), service_account_id=None)


def _kwargs(pat=_PAT, repos=(_REPO,)):
    return {
        "tenant": _tenant(),
        "credential_vault": _FakeVault(pat),
        "session_config": {"repos": [dict(r) for r in repos]},
    }


@respx.mock
async def test_lists_issues_and_carries_pat_in_header():
    route = respx.get(url__regex=r"https://api\.github\.com/repos/acme/api/issues").mock(
        return_value=httpx.Response(200, json=[{"number": 6, "title": "Bug"}]),
    )
    out = await _github_handler(
        {"path": "/repos/acme/api/issues", "params": {"state": "open"}}, **_kwargs(),
    )
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {_PAT}"
    assert req.url.params["state"] == "open"

    data = json.loads(out)
    assert data["status"] == 200
    assert data["data"][0]["title"] == "Bug"
    # The token never leaks into the tool result.
    assert _PAT not in out


async def test_no_pat_returns_guidance():
    out = await _github_handler(
        {"path": "/repos/acme/api/issues"}, **_kwargs(pat=None),
    )
    assert json.loads(out)["code"] == "git_pat_not_connected"


async def test_no_repos_returns_guidance():
    out = await _github_handler(
        {"path": "/repos/acme/api/issues"}, **_kwargs(repos=()),
    )
    assert json.loads(out)["code"] == "repo_not_configured"


@respx.mock
async def test_out_of_scope_repo_denied_without_calling_github():
    route = respx.get(url__regex=r"https://api\.github\.com/.*").mock(
        return_value=httpx.Response(200, json={}),
    )
    out = await _github_handler(
        {"path": "/repos/other/secret/issues"}, **_kwargs(),
    )
    assert json.loads(out)["code"] == "out_of_scope"
    assert not route.called  # never hit GitHub for an unconfigured repo


@respx.mock
async def test_github_error_message_surfaced():
    respx.get(url__regex=r"https://api\.github\.com/repos/acme/api/pulls/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"}),
    )
    out = await _github_handler(
        {"path": "/repos/acme/api/pulls/999"}, **_kwargs(),
    )
    data = json.loads(out)
    assert data["status"] == 404
    assert data["error"] == "Not Found"


@respx.mock
async def test_search_scoped_to_configured_repo():
    route = respx.get(url__regex=r"https://api\.github\.com/search/issues").mock(
        return_value=httpx.Response(200, json={"total_count": 2, "items": []}),
    )
    out = await _github_handler(
        {"path": "/search/issues", "params": {"q": "repo:acme/api is:open"}}, **_kwargs(),
    )
    assert route.called
    assert json.loads(out)["data"]["total_count"] == 2


@respx.mock
async def test_diff_media_type_sets_accept_header():
    route = respx.get(url__regex=r"https://api\.github\.com/repos/acme/api/commits/abc").mock(
        return_value=httpx.Response(200, text="diff --git a/x b/x"),
    )
    out = await _github_handler(
        {"path": "/repos/acme/api/commits/abc", "media_type": "diff"}, **_kwargs(),
    )
    assert route.calls.last.request.headers["Accept"] == "application/vnd.github.diff"
    assert "diff --git" in json.loads(out)["data"]
