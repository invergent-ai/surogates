"""The GitHub tool is scoped to the agent's configured repositories."""

from surogates.tools.builtin.github import authorize_github_request, github_repo_slugs

REPOS = [
    {"url": "https://github.com/acme/api", "default_branch": "main"},
    {"url": "https://github.com/acme/web.git", "default_branch": "main"},
]


def test_slugs_parsed_from_repo_urls():
    assert github_repo_slugs(REPOS) == {"acme/api", "acme/web"}


def test_repos_path_for_configured_repo_allowed():
    for path in (
        "/repos/acme/api",
        "/repos/acme/api/issues",
        "/repos/acme/api/issues?state=open",
        "/repos/acme/web/contents/README.md",
        "/repos/acme/web/commits",
        "/repos/acme/api/pulls/42/files",
    ):
        ok, _ = authorize_github_request("GET", path, None, REPOS)
        assert ok, path


def test_repos_path_case_insensitive():
    ok, _ = authorize_github_request("GET", "/repos/ACME/API/issues", None, REPOS)
    assert ok


def test_repos_path_for_unconfigured_repo_denied():
    ok, reason = authorize_github_request("GET", "/repos/other/secret/issues", None, REPOS)
    assert not ok
    assert "not a configured repository" in reason


def test_search_scoped_to_configured_repo_allowed():
    ok, _ = authorize_github_request(
        "GET", "/search/issues", {"q": "repo:acme/api is:issue is:open"}, REPOS,
    )
    assert ok


def test_search_without_repo_qualifier_denied():
    ok, reason = authorize_github_request("GET", "/search/issues", {"q": "is:open bug"}, REPOS)
    assert not ok
    assert "repo:" in reason


def test_search_with_unconfigured_repo_denied():
    ok, reason = authorize_github_request(
        "GET", "/search/issues", {"q": "repo:other/secret is:open"}, REPOS,
    )
    assert not ok
    assert "other/secret" in reason


def test_search_with_broadening_qualifier_denied():
    # org:/user: would search beyond a single configured repo.
    ok, reason = authorize_github_request("GET", "/search/code", {"q": "org:acme foo"}, REPOS)
    assert not ok


def test_non_repo_paths_denied():
    for path in ("/user", "/orgs/acme/repos", "/user/repos", "/repos"):
        ok, _ = authorize_github_request("GET", path, None, REPOS)
        assert not ok, path


def test_no_configured_repos_denies_everything():
    ok, reason = authorize_github_request("GET", "/repos/acme/api/issues", None, [])
    assert not ok


# --- writes -----------------------------------------------------------------


def test_write_to_sub_resource_of_configured_repo_allowed():
    for method, path in (
        ("POST", "/repos/acme/api/issues"),
        ("POST", "/repos/acme/api/issues/5/comments"),
        ("PATCH", "/repos/acme/api/issues/5"),
        ("PUT", "/repos/acme/api/contents/notes.md"),
        ("POST", "/repos/acme/api/pulls/7/reviews"),
        ("PUT", "/repos/acme/api/pulls/7/merge"),
        ("DELETE", "/repos/acme/api/issues/comments/99"),
    ):
        ok, reason = authorize_github_request(method, path, None, REPOS)
        assert ok, f"{method} {path}: {reason}"


def test_write_to_unconfigured_repo_denied():
    ok, reason = authorize_github_request("POST", "/repos/other/secret/issues", None, REPOS)
    assert not ok
    assert "not a configured repository" in reason


def test_write_to_repo_root_denied():
    # Deleting or reconfiguring the repository itself is refused.
    for method in ("DELETE", "PATCH", "PUT", "POST"):
        ok, reason = authorize_github_request(method, "/repos/acme/api", None, REPOS)
        assert not ok, method
        assert "repository-level" in reason


def test_write_to_protected_admin_subresource_denied():
    ok, reason = authorize_github_request("POST", "/repos/acme/api/transfer", None, REPOS)
    assert not ok
    assert "transfer" in reason


def test_reading_repo_root_still_allowed():
    # GET on the repo root remains fine — only writes to it are blocked.
    ok, _ = authorize_github_request("GET", "/repos/acme/api", None, REPOS)
    assert ok


def test_write_to_search_denied():
    ok, reason = authorize_github_request(
        "POST", "/search/issues", {"q": "repo:acme/api is:open"}, REPOS,
    )
    assert not ok
    assert "read-only" in reason
