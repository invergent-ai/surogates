"""Channel-deliverable code-run progress text."""

from surogates.coding_agents.run_progress import (
    render_code_run_ack,
    render_code_run_update,
)

REPO = {"url": "https://github.com/acme/api", "default_branch": "main"}


def test_ack_names_agent_and_repo():
    s = render_code_run_ack("claude", REPO)
    assert "claude" in s
    assert "acme/api" in s


def test_ack_without_repo_is_still_valid():
    s = render_code_run_ack("codex", None)
    assert "codex" in s
    assert "on `" not in s  # no dangling repo mention


def test_update_includes_repo_and_activity_hint():
    s = render_code_run_update(REPO, "Editing search.py to add fuzzy match\n› Bash")
    assert "acme/api" in s
    assert "Editing search.py" in s  # the prose line, not the › tool marker


def test_update_skips_bare_tool_markers():
    s = render_code_run_update(REPO, "› Bash\n› TaskOutput")
    assert "acme/api" in s
    assert "›" not in s  # nothing but markers → no activity tail


def test_update_truncates_long_activity():
    s = render_code_run_update(REPO, "x" * 500)
    assert len(s) < 400


def test_update_empty_activity_ok():
    s = render_code_run_update(REPO, "")
    assert "acme/api" in s
