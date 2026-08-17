"""Verify session workspace keys are scoped under storage_key_prefix."""

from surogates.storage.tenant import (
    prefixed_session_workspace_key,
    prefixed_session_workspace_prefix,
    storage_key_prefix,
)


def test_storage_key_prefix_returns_empty_for_none_config():
    assert storage_key_prefix(None) == ""


def test_storage_key_prefix_returns_empty_for_missing_key():
    assert storage_key_prefix({}) == ""


def test_storage_key_prefix_returns_value_when_set():
    assert storage_key_prefix({"storage_key_prefix": "p-1/a-1"}) == "p-1/a-1"


def test_storage_key_prefix_coerces_none_to_empty():
    """Older sessions may stamp the field as None — normalise to ''."""
    assert storage_key_prefix({"storage_key_prefix": None}) == ""


def test_prefixed_session_workspace_prefix_without_storage_prefix():
    assert prefixed_session_workspace_prefix({}, "s-1") == "s-1/"


def test_prefixed_session_workspace_prefix_with_storage_prefix():
    assert (
        prefixed_session_workspace_prefix({"storage_key_prefix": "p-1/a-1"}, "s-1")
        == "p-1/a-1/s-1/"
    )


def test_prefixed_session_workspace_key_without_storage_prefix():
    assert (
        prefixed_session_workspace_key({}, "s-1", "docs/readme.md")
        == "s-1/docs/readme.md"
    )


def test_prefixed_session_workspace_key_with_storage_prefix():
    assert (
        prefixed_session_workspace_key(
            {"storage_key_prefix": "p-1/a-1"},
            "s-1",
            "docs/readme.md",
        )
        == "p-1/a-1/s-1/docs/readme.md"
    )


def test_prefixed_session_workspace_key_empty_key():
    """Empty key collapses to the workspace prefix without a trailing slash artefact."""
    assert (
        prefixed_session_workspace_key(
            {"storage_key_prefix": "p-1/a-1"}, "s-1", "",
        )
        == "p-1/a-1/s-1/"
    )


from types import SimpleNamespace

from surogates.storage.tenant import (
    boundary_workspace_key,
    boundary_workspace_prefix,
    workspace_boundary,
)


def _boundary_session(channel: str, config: dict, sid: str = "session-1"):
    return SimpleNamespace(channel=channel, config=config, id=sid)


def test_workspace_boundary_prefers_pinned_workspace_boundary():
    session = _boundary_session(
        "worker", {"workspace_boundary": "slack:c:G1", "memory_boundary": "wrong"},
    )
    assert workspace_boundary(session) == "slack:c:G1"


def test_workspace_boundary_uses_managed_channel_memory_boundary():
    session = _boundary_session("slack", {"memory_boundary": "slack:c:G1"})
    assert workspace_boundary(session) == "slack:c:G1"


def test_workspace_boundary_ignores_non_channel_memory_boundary():
    session = _boundary_session("web", {"memory_boundary": "slack:c:G1"})
    assert workspace_boundary(session) is None


def test_eval_rows_of_one_run_do_not_share_a_workspace():
    # An eval boundary partitions memory only. Two rows of the same run must
    # keep separate workspaces, or a file the first row writes shows up in the
    # second and they contaminate each other.
    config = {"memory_boundary": "eval:run-1", "storage_key_prefix": "p/a"}
    row_one = _boundary_session("api", dict(config), sid="row-1")
    row_two = _boundary_session("api", dict(config), sid="row-2")

    assert workspace_boundary(row_one) is None
    prefix_one = boundary_workspace_prefix(config, row_one, "row-1")
    prefix_two = boundary_workspace_prefix(config, row_two, "row-2")
    assert prefix_one != prefix_two
    assert "eval:run-1" not in prefix_one


def test_managed_channel_workspace_boundary_is_unaffected():
    session = _boundary_session("slack", {"memory_boundary": "slack:c:G1"})
    assert workspace_boundary(session) == "slack:c:G1"
    assert boundary_workspace_prefix(
        {"storage_key_prefix": "p/a"}, session, "session-1",
    ) == "p/a/boundaries/slack:c:G1/workspace/"


def test_boundary_workspace_prefix_uses_boundary_with_trailing_slash():
    session = _boundary_session("slack", {"memory_boundary": "slack:c:G1"})
    assert (
        boundary_workspace_prefix(
            {"storage_key_prefix": "project/agent"}, session, "root-session",
        )
        == "project/agent/boundaries/slack:c:G1/workspace/"
    )


def test_boundary_workspace_prefix_falls_back_to_session_prefix():
    session = _boundary_session("web", {"memory_boundary": "ignored"})
    assert (
        boundary_workspace_prefix(
            {"storage_key_prefix": "project/agent"}, session, "root-session",
        )
        == "project/agent/root-session/"
    )


def test_boundary_workspace_prefix_fail_closes_older_managed_session():
    session = _boundary_session(
        "telegram", {"channel_session_key": "agent:telegram:group:-100"},
        sid="legacy-session",
    )
    assert (
        boundary_workspace_prefix(
            {"storage_key_prefix": "project/agent"}, session, "legacy-session",
        )
        == "project/agent/boundaries/telegram:iso:agent:telegram:group:-100/workspace/"
    )


def test_boundary_workspace_key_strips_leading_slash():
    session = _boundary_session("slack", {"memory_boundary": "slack:c:G1"})
    assert (
        boundary_workspace_key(
            {"storage_key_prefix": "project/agent"}, session, "root-session",
            "/docs/report.pdf",
        )
        == "project/agent/boundaries/slack:c:G1/workspace/docs/report.pdf"
    )
