"""Paths as the model writes them, made relative to the workspace."""

from types import SimpleNamespace

from surogates.office.agent import agent_user_id, relative_path


def test_paths_lose_the_workspace_root_and_the_sanitized_marker():
    session = SimpleNamespace(config={"workspace_path": "/workspace"})
    assert relative_path("/workspace/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("__WORKSPACE__/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("docs/a.docx", session) == "docs/a.docx"


def test_the_agent_signs_in_per_chat():
    assert agent_user_id("writer", "5f0c") == "agent:writer:5f0c"
