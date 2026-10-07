"""A local folder's skills: their files put in the folder by the tool call that views them."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.storage.skill_staging import stage_in_folder
from surogates.tools.builtin.skills import _skill_view_handler
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner

pytestmark = pytest.mark.asyncio


class Api:
    """The api client's two skill calls: the skill as the api has it, and its files' bytes."""

    def __init__(self, files: dict[str, bytes], name: str | None = None) -> None:
        self.files = files
        self.name = name  # the skill's own name, as the api answers it
        self.fetched: list[str] = []

    async def view_skill(self, name, file_path=None) -> str:
        return json.dumps({
            "success": True, "name": self.name or name, "content": "Run scripts/build.py.\n",
            "linked_files": sorted(self.files),
        })

    async def skill_file_bytes(self, name, path) -> bytes:
        self.fetched.append(path)
        return self.files[path]


@pytest.fixture
def folder(tmp_path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def runner(folder) -> InProcessRunner:
    return InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder)))


@pytest.fixture
def files(runner, folder) -> DeviceWorkspaceIO:
    return DeviceWorkspaceIO(runner, root=str(folder))


def on_a_computer(folder) -> dict:
    return {"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": str(folder)}


async def test_a_skills_files_are_put_in_the_folder_once_for_the_chat(files, runner, folder):
    api, chat = Api({"scripts/build.py": b"print('x')", "assets/t.pptx": b"\x89PK"}), str(uuid4())

    async def stage(owner: str) -> str:
        return await stage_in_folder(
            files, skill_name="deck", linked_files=sorted(api.files), owner=owner,
            fetch=lambda path: api.skill_file_bytes("deck", path),
        )

    assert await stage(chat) == ".surogates-results/skills/deck"
    staged = folder / ".surogates-results" / "skills" / "deck"
    assert (staged / "assets" / "t.pptx").read_bytes() == b"\x89PK"
    assert (staged / "scripts" / "build.py").read_bytes() == b"print('x')"
    writes = runner.kinds.count("write")
    # Its sub-agents, and its next view, find them there.
    await stage(chat)
    assert runner.kinds.count("write") == writes
    # A later chat on the same folder puts them there afresh: the skill may have changed.
    api.files["scripts/build.py"] = b"print('y')"
    await stage(str(uuid4()))
    assert (staged / "scripts" / "build.py").read_bytes() == b"print('y')"


async def test_skill_view_on_a_local_folder_stages_through_its_call_and_names_the_folder(files, folder):
    api = Api({"scripts/build.py": b"print('x')"})
    payload = json.loads(await _skill_view_handler(
        {"name": "deck"}, api_client=api, workspace_io=files, session_config=on_a_computer(folder), task_id=str(uuid4()),
    ))
    staged_at = f"{folder}/.surogates-results/skills/deck/"
    assert payload["staged_at"] == staged_at
    assert payload["content"].startswith(f"> **Skill staging.** This skill's files live at `{staged_at[:-1]}/`")
    assert f"working directory is `{folder}`" in payload["content"]
    assert payload["content"].endswith("Run scripts/build.py.\n")
    assert (folder / ".surogates-results" / "skills" / "deck" / "scripts" / "build.py").is_file()


async def test_outside_a_tool_call_a_local_folders_skill_says_how_to_have_its_files(folder):
    api = Api({"scripts/build.py": b"print('x')"})
    # A slash command's expansion: no call to put them in the folder under.
    payload = json.loads(await _skill_view_handler(
        {"name": "deck"}, api_client=api, session_config=on_a_computer(folder),
    ))
    assert "staged_at" not in payload
    assert payload["content"].endswith('call skill_view("deck") to put them there.')
    assert api.fetched == []
    assert not (folder / ".surogates-results").exists()


async def test_a_skill_is_staged_under_its_own_name_whatever_the_call_spelled(files, folder):
    api = Api({"scripts/build.py": b"print('x')"}, name="deck")
    # The api's path drops the dot segments: the call's spelling is no folder name.
    payload = json.loads(await _skill_view_handler(
        {"name": "../../src/../v1/skills/deck"}, api_client=api, workspace_io=files,
        session_config=on_a_computer(folder), task_id=str(uuid4()),
    ))
    assert payload["staged_at"] == f"{folder}/.surogates-results/skills/deck/"
    written = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())
    assert ".surogates-results/skills/deck/scripts/build.py" in written
    assert all(path.startswith(".surogates-results/") for path in written), written


@pytest.mark.parametrize(("name", "linked"), [
    ("../deck", ["a.txt"]), ("a/b", ["a.txt"]), (".hidden", ["a.txt"]), ("", ["a.txt"]),
    ("deck", ["../../README.md"]), ("deck", ["scripts/../../x"]), ("deck", ["/etc/passwd"]),
])
async def test_staging_refuses_a_name_or_a_file_that_would_leave_its_folder(files, folder, name, linked):
    (folder / "README.md").write_text("mine")

    async def fetch(path: str) -> bytes:
        return b"theirs"

    with pytest.raises(ValueError, match="cannot be put in the folder"):
        await stage_in_folder(files, skill_name=name, linked_files=linked, owner="root", fetch=fetch)
    assert sorted(path.name for path in folder.rglob("*")) == ["README.md"]
    assert (folder / "README.md").read_text() == "mine"


async def test_a_skills_files_are_fetched_for_this_chat_and_a_failure_names_only_the_file():
    import httpx

    from surogates.harness.api_client import HarnessAPIClient

    asked: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if request.url.params["path"] == "scripts/gone.py":
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, content=b"print('x')")

    api = HarnessAPIClient("http://api.internal:8000", "token", session_id="chat-1", agent_id="agent-7")
    api._client = httpx.AsyncClient(base_url="http://api.internal:8000", transport=httpx.MockTransport(answer))
    assert await api.skill_file_bytes("deck", "scripts/build.py") == b"print('x')"
    # The chat's own entitlement, as view_skill asks for it.
    assert dict(asked[0].url.params) == {
        "agent_id": "agent-7", "session_id": "chat-1", "path": "scripts/build.py", "raw": "true",
    }
    with pytest.raises(httpx.HTTPError) as failed:
        await api.skill_file_bytes("deck", "scripts/gone.py")
    # What the model reads: never the api's address or the agent's id.
    assert str(failed.value) == "the skill's file scripts/gone.py could not be fetched (HTTP 404)"
