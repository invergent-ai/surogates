"""The GitHub read tool is scoped to the agent's configured repositories."""

from surogates.tools.builtin.github import authorize_github_read, github_repo_slugs

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
        ok, _ = authorize_github_read(path, None, REPOS)
        assert ok, path


def test_repos_path_case_insensitive():
    ok, _ = authorize_github_read("/repos/ACME/API/issues", None, REPOS)
    assert ok


def test_repos_path_for_unconfigured_repo_denied():
    ok, reason = authorize_github_read("/repos/other/secret/issues", None, REPOS)
    assert not ok
    assert "not a configured repository" in reason


def test_search_scoped_to_configured_repo_allowed():
    ok, _ = authorize_github_read(
        "/search/issues", {"q": "repo:acme/api is:issue is:open"}, REPOS,
    )
    assert ok


def test_search_without_repo_qualifier_denied():
    ok, reason = authorize_github_read("/search/issues", {"q": "is:open bug"}, REPOS)
    assert not ok
    assert "repo:" in reason


def test_search_with_unconfigured_repo_denied():
    ok, reason = authorize_github_read(
        "/search/issues", {"q": "repo:other/secret is:open"}, REPOS,
    )
    assert not ok
    assert "other/secret" in reason


def test_search_with_broadening_qualifier_denied():
    # org:/user: would search beyond a single configured repo.
    ok, reason = authorize_github_read("/search/code", {"q": "org:acme foo"}, REPOS)
    assert not ok


def test_non_repo_paths_denied():
    for path in ("/user", "/orgs/acme/repos", "/user/repos", "/repos"):
        ok, _ = authorize_github_read(path, None, REPOS)
        assert not ok, path


def test_no_configured_repos_denies_everything():
    ok, reason = authorize_github_read("/repos/acme/api/issues", None, [])
    assert not ok
