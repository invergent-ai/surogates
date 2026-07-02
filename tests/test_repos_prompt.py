"""The agent's configured repos are rendered into a system-prompt section."""

from surogates.coding_agents.repo_resolve import render_repos_prompt

REPOS = [
    {"url": "https://github.com/invergent-ai/surogate-ops.git", "default_branch": "main"},
    {"url": "https://github.com/invergent-ai/surogates", "default_branch": "master"},
]


def test_no_repos_renders_nothing():
    assert render_repos_prompt([]) == ""
    assert render_repos_prompt(None) == ""


def test_lists_each_repo_slug_and_default_branch():
    s = render_repos_prompt(REPOS)
    assert "invergent-ai/surogate-ops" in s
    assert "invergent-ai/surogates" in s
    assert "main" in s and "master" in s


def test_points_at_the_tools():
    s = render_repos_prompt(REPOS)
    assert "github" in s  # the read tool
    assert "run_coding_agent" in s or "/code" in s  # the write path


def test_instructs_to_include_links():
    s = render_repos_prompt(REPOS)
    assert "link" in s.lower()
    assert "https://github.com" in s
    # names the artifact kinds the user asked to be linked
    low = s.lower()
    assert "issue" in low and "commit" in low and "pull request" in low


def test_skips_malformed_urls():
    assert render_repos_prompt([{"url": "not-a-url", "default_branch": "x"}]) == ""
