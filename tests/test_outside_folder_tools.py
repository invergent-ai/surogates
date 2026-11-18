"""The tools that work on files outside a desktop chat's folder: each asks the computer, which shows its user a window."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.harness.loop import AgentHarness
from surogates.harness.tool_schemas import drop_unusable_tools, unusable_tool_names
from surogates.tools.builtin import outside_folder
from surogates.tools.builtin.outside_folder import IN_A_THREAD, OUTSIDE_FOLDER_TOOLS
from surogates.tools.registry import ToolRegistry
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
from tests.test_device_browser_tools import Laptop
from tests.test_device_tool_exec import device_session, run


class OutsideLaptop(Laptop):
    """A computer in a test whose answers to outside.* kinds are scripted."""

    def __init__(self, folder: str, *answers: dict[str, Any]) -> None:
        super().__init__(folder)
        self.answers = list(answers)

    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        if not kind.startswith("outside."):
            return await super().run(kind, args, payload)
        self.asked.append((kind, json.loads(json.dumps(args))))
        return self.answers.pop(0)


FOLDER = "/home/ana/proj"


def on_the_computer(*answers: dict[str, Any]) -> tuple[OutsideLaptop, dict[str, Any]]:
    laptop = OutsideLaptop(FOLDER, *answers)
    return laptop, {
        "workspace_io": DeviceWorkspaceIO(laptop, root=FOLDER),
        "workspace_path": None,
        "session_id": uuid4(),
        "session_config": {"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": FOLDER},
    }


async def call(name: str, arguments: dict[str, Any], **kwargs: Any) -> str:
    registry = ToolRegistry()
    outside_folder.register(registry)
    return await registry.get(name).handler(arguments, **kwargs)


async def test_request_folder_access_asks_for_a_known_folder_or_an_absolute_path():
    laptop, kwargs = on_the_computer({"ok": {"folder": "/mnt/c/Users/Ana/Desktop"}}, {"ok": {"folder": "/mnt/d/Scans"}})

    assert json.loads(await call("request_folder_access", {"folder": "Desktop"}, **kwargs)) == {
        "folder": "/mnt/c/Users/Ana/Desktop",
    }
    assert json.loads(await call("request_folder_access", {"folder": "/mnt/d/Scans/"}, **kwargs)) == {
        "folder": "/mnt/d/Scans",
    }
    assert laptop.asked == [
        ("outside.grant", {"folder": "Desktop"}),
        ("outside.grant", {"folder": "/mnt/d/Scans"}),
    ]


async def test_copy_files_sends_absolute_paths_with_the_chats_folder_for_dot():
    copied = {"copied": [{"from": "/mnt/d/Scans/a.pdf", "to": f"{FOLDER}/a.pdf"}], "skipped": []}
    laptop, kwargs = on_the_computer({"ok": copied}, {"ok": {"copied": [], "skipped": []}})

    assert json.loads(await call("copy_files", {"files": ["/mnt/d/Scans/a.pdf"], "to": "."}, **kwargs)) == copied
    await call("copy_files", {"files": ["notes/b.txt", "./c.txt"], "to": "/mnt/d/Scans/out/"}, **kwargs)

    assert laptop.asked == [
        ("outside.copy", {"files": ["/mnt/d/Scans/a.pdf"], "to": FOLDER}),
        ("outside.copy", {"files": [f"{FOLDER}/notes/b.txt", f"{FOLDER}/c.txt"], "to": "/mnt/d/Scans/out"}),
    ]


async def test_move_to_trash_sends_the_files_and_answers_what_was_trashed():
    trashed = {"trashed": ["/mnt/d/Scans/a.pdf"], "kept": [{"file": "/mnt/d/Scans/b.pdf", "reason": "unticked"}]}
    laptop, kwargs = on_the_computer({"ok": trashed})

    result = await call("move_to_trash", {"files": ["/mnt/d/Scans/a.pdf", "/mnt/d/Scans/b.pdf"]}, **kwargs)

    assert json.loads(result) == trashed
    assert laptop.asked == [("outside.trash", {"files": ["/mnt/d/Scans/a.pdf", "/mnt/d/Scans/b.pdf"]})]


async def test_put_back_sends_the_copy_by_its_full_path_and_answers_a_conflict_in_its_words():
    laptop, kwargs = on_the_computer(
        {"ok": {"original": "/mnt/d/Scans/a.docx", "copyDeleted": True}},
        {"error": {"type": "conflict", "message": "The original changed since it was copied."}},
    )

    done = await call("put_back", {"working_copy": "a.docx", "original": "/mnt/d/Scans/a.docx"}, **kwargs)
    refused = await call("put_back", {"working_copy": "a.docx", "original": "/mnt/d/Scans/a.docx"}, **kwargs)

    assert json.loads(done) == {"original": "/mnt/d/Scans/a.docx", "copyDeleted": True}
    assert json.loads(refused) == {"error": "The original changed since it was copied."}
    assert laptop.asked == [("outside.put_back", {"copy": f"{FOLDER}/a.docx", "original": "/mnt/d/Scans/a.docx"})] * 2


async def test_a_no_and_an_old_app_are_answered_as_the_tools_result():
    _laptop, kwargs = on_the_computer(
        {"error": {"type": "denied", "message": "The user said no."}},
        {"error": {"type": "unsupported", "message": "This computer does not handle outside.grant yet"}},
    )

    assert json.loads(await call("request_folder_access", {"folder": "Downloads"}, **kwargs)) == {
        "error": "The user said no.",
    }
    assert json.loads(await call("request_folder_access", {"folder": "Downloads"}, **kwargs)) == {
        "error": "The Surogate app on this computer cannot work outside the chat's folder yet. Update it, then try again.",
    }


async def test_a_chat_in_the_cloud_is_refused_and_asks_nothing():
    result = await call("copy_files", {"files": ["/a"], "to": "."}, workspace_io=None, session_config={})
    assert "error" in json.loads(result)


@pytest.mark.parametrize("arguments", [{"files": "a.txt", "to": "."}, {"files": ["a.txt"]}, {"files": [], "to": "."}])
async def test_malformed_arguments_ask_nothing(arguments):
    laptop, kwargs = on_the_computer()
    assert "error" in json.loads(await call("copy_files", arguments, **kwargs))
    assert laptop.asked == []


def test_the_tools_run_in_the_worker():
    assert all(TOOL_LOCATIONS[name] is ToolLocation.HARNESS for name in OUTSIDE_FOLDER_TOOLS)


def _harness(session_config: dict) -> tuple[Any, Any]:
    harness = SimpleNamespace(_prompt=SimpleNamespace(has_kbs=True), _slash_command_enabled=lambda *a: True)
    session = SimpleNamespace(
        parent_id=None, user_id=uuid4(), service_account_id=None, channel="web", config=session_config,
    )
    return harness, session


@pytest.mark.parametrize("on_device", [True, False])
def test_offers_the_outside_tools_only_in_a_device_chat(on_device):
    config = {"execution": {"kind": "device", "device_id": str(uuid4())}} if on_device else {}
    harness, session = _harness(config)
    usable = AgentHarness._tool_usability(harness, session, ())
    assert usable["on_device"] is on_device

    schemas = [{"function": {"name": name}} for name in (*sorted(OUTSIDE_FOLDER_TOOLS), "read_file")]
    kept = {s["function"]["name"] for s in drop_unusable_tools(schemas, **usable)}
    assert kept == ({*OUTSIDE_FOLDER_TOOLS, "read_file"} if on_device else {"read_file"})
    assert (OUTSIDE_FOLDER_TOOLS & unusable_tool_names(**usable)) == (frozenset() if on_device else OUTSIDE_FOLDER_TOOLS)


@pytest.mark.parametrize("name", sorted(OUTSIDE_FOLDER_TOOLS))
async def test_refuses_the_outside_tools_in_a_projects_thread(name):
    registry = ToolRegistry()
    outside_folder.register(registry)
    called: list[str] = []

    async def handler(arguments, **kwargs):
        called.append(name)
        return "{}"

    registry.get(name).handler = handler
    result = await run(registry, device_session(workstream_role="thread"), name, {"folder": "Desktop"})

    assert called == []
    assert IN_A_THREAD in json.dumps(result)
    assert IN_A_THREAD == "Files outside the chat's folder can be used from the main chat, not from a project's thread."


async def test_the_office_tools_send_a_document_outside_the_folder_to_copy_it_in_first(monkeypatch):
    from surogates.office import guard
    from surogates.office.agent import OUTSIDE_DOCUMENT
    from surogates.tools.builtin import office

    session = device_session()
    store = SimpleNamespace(get_session=lambda _id: _now(session))
    kwargs = {"session_store": store, "session_id": session.id}

    for handler in (office._office_read, office._office_edit):
        answer = json.loads(await handler({"path": "/mnt/d/Scans/a.docx", "ops": [{"op": "delete"}]}, **kwargs))
        assert answer["error"] == OUTSIDE_DOCUMENT
    # A path through the computer's home folder is answered as before.
    answer = json.loads(await office._office_read({"path": "~/a.docx"}, **kwargs))
    assert answer["error"] == "Name the document by its path in the chat's folder, like docs/report.docx."

    monkeypatch.setattr(guard, "_applies", lambda *a: True)
    said = await guard.refused_open_document(
        "write_file", {"path": "/mnt/d/Scans/a.docx", "content": "x"}, session, session_factory=object(),
    )
    assert said == f"/mnt/d/Scans/a.docx: {OUTSIDE_DOCUMENT}"


async def _now(value):
    return value
