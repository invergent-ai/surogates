"""Paths as the model writes them, made relative to the workspace."""

from types import SimpleNamespace

from surogates.office.agent import agent_user_id, relative_path


def test_paths_lose_the_workspace_root_and_the_sanitized_marker():
    session = SimpleNamespace(config={"workspace_path": "/workspace"})
    assert relative_path("/workspace/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("__WORKSPACE__/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("docs/a.docx", session) == "docs/a.docx"


def test_paths_through_home_or_with_dots_and_double_slashes_name_the_same_file():
    # The sandbox's HOME is the workspace.
    session = SimpleNamespace(config={"workspace_path": "/workspace"})
    for path in ("~/a.docx", "$HOME/a.docx", "${HOME}/a.docx", "./a.docx", "__WORKSPACE__/a.docx", "/workspace/./a.docx"):
        assert relative_path(path, session) == "a.docx", path
    assert relative_path("docs//a.docx", session) == "docs/a.docx"
    assert relative_path("/workspace/docs/a.docx", session) == "docs/a.docx"
    assert relative_path("~x.docx", session) == "~x.docx"
    assert relative_path("$HOMEx/a.docx", session) == "$HOMEx/a.docx"
    assert relative_path("/workspacex/a.docx", session) == "/workspacex/a.docx"
    for root in ("~", "~/", "/workspace", "/workspace/", ".", ""):
        assert relative_path(root, session) == ".", root
    # A way out stays one, for the workspace to refuse.
    assert relative_path("~/../etc/a.docx", session) == "../etc/a.docx"


def test_the_agent_signs_in_per_chat():
    assert agent_user_id("writer", "5f0c") == "agent:writer:5f0c"
