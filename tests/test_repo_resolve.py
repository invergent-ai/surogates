"""Tests for repo selection + git-PAT resolution for a coding run."""

from __future__ import annotations

from surogates.coding_agents.repo_resolve import (
    principal_kwargs,
    resolve_git_pat,
    select_repo,
)

REPOS = [
    {"url": "https://github.com/acme/api", "default_branch": "main"},
    {"url": "https://github.com/acme/web", "default_branch": "main"},
]


def test_select_by_name():
    assert select_repo(REPOS, "web")["url"].endswith("/web")


def test_select_sole_when_no_request():
    assert select_repo(REPOS[:1], None)["url"].endswith("/api")


def test_ambiguous_returns_none():
    assert select_repo(REPOS, None) is None  # >1 repo, no pick


def test_no_repos_returns_none():
    assert select_repo([], "api") is None


def test_unmatched_request_returns_none():
    assert select_repo(REPOS, "nope") is None


def test_select_accepts_full_url_or_git_suffix():
    assert select_repo(REPOS, "https://github.com/acme/web")["url"].endswith("/web")
    assert select_repo(REPOS, "web.git")["url"].endswith("/web")


def test_select_returns_independent_copy():
    repos = [{"url": "https://github.com/acme/api", "default_branch": "main"}]
    picked = select_repo(repos, None)
    picked["url"] = "mutated"
    assert repos[0]["url"] == "https://github.com/acme/api"


def test_principal_kwargs_prefers_service_account():
    assert principal_kwargs(user_id="u", service_account_id="sa") == {
        "user_id": None,
        "service_account_id": "sa",
    }


def test_principal_kwargs_falls_back_to_user():
    assert principal_kwargs(user_id="u", service_account_id=None) == {
        "user_id": "u",
        "service_account_id": None,
    }


class _FakeVault:
    def __init__(self, value=None):
        self.value = value
        self.calls = []

    async def resolve_ref(self, ref, *, org_id, user_id=None, service_account_id=None):
        self.calls.append(
            {"ref": ref, "org_id": org_id, "user_id": user_id,
             "service_account_id": service_account_id},
        )
        return self.value


async def test_resolve_git_pat_uses_service_account_principal():
    vault = _FakeVault("github_pat_x")
    pat = await resolve_git_pat(
        vault, org_id="o", user_id="u", service_account_id="sa",
    )
    assert pat == "github_pat_x"
    call = vault.calls[0]
    assert call["ref"] == "vault://git_pat"
    assert call["org_id"] == "o"
    # Service account wins; user is dropped (no cross-principal read).
    assert call["service_account_id"] == "sa"
    assert call["user_id"] is None


async def test_resolve_git_pat_uses_user_when_no_service_account():
    vault = _FakeVault(None)
    pat = await resolve_git_pat(vault, org_id="o", user_id="u")
    assert pat is None
    call = vault.calls[0]
    assert call["user_id"] == "u"
    assert call["service_account_id"] is None
