"""Paths as the model writes them, made relative to the workspace."""

from types import SimpleNamespace

from surogates.office.agent import agent_user_id, relative_path, within_folder


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


def test_on_a_computer_home_is_its_users_not_the_folder():
    session = SimpleNamespace(config={
        "workspace_path": "/home/me/notes", "execution": {"kind": "device", "device_id": "00000000-0000-0000-0000-000000000001"},
    })
    for path in ("~/a.docx", "$HOME/a.docx", "${HOME}/a.docx"):
        assert relative_path(path, session) == path
    assert relative_path("/home/me/notes/a.docx", session) == "a.docx"
    assert relative_path("__WORKSPACE__/a.docx", session) == "a.docx"


def test_within_folder_is_a_relative_path_that_stays_in():
    for path in ("a.docx", "docs/a.docx"):
        assert within_folder(path), path
    for path in ("~/a.docx", "$HOME/a.docx", "/etc/a.docx", "..", "../a.docx"):
        assert not within_folder(path), path


def test_the_agent_signs_in_per_chat():
    assert agent_user_id("writer", "5f0c") == "agent:writer:5f0c"
