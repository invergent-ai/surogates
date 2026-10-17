"""A terminal call's result says when it left office documents behind, or named one the editor holds."""

from __future__ import annotations

import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.devices.operations import DeviceOperations
from surogates.harness.tool_exec import execute_single_tool
from surogates.office import guard
from surogates.office import sessions as office_sessions
from surogates.office.scripted import SCRIPTED_NOTE, SCRIPTED_PDF_NOTE
from surogates.tools import skill_visibility
from surogates.tools.registry import ToolRegistry, ToolSchema

pytestmark = pytest.mark.asyncio

RAN = json.dumps({"output": "done", "exit_code": 0, "error": None})
_ids = iter(range(1, 1_000_000))


@pytest.fixture(autouse=True)
def office_on(monkeypatch):
    on = lambda: SimpleNamespace(office=SimpleNamespace(agent_editing=True))  # noqa: E731
    monkeypatch.setattr(skill_visibility, "load_settings", on)
    monkeypatch.setattr(guard, "load_settings", on)
    # The consumed mark after each committed result on a computer, over a MagicMock session factory.
    monkeypatch.setattr(DeviceOperations, "consume", AsyncMock(return_value=0))


class Pod:
    """A sandbox whose terminal runs the call, and whose find lists *changed*."""

    def __init__(self, changed: str = "") -> None:
        self.changed = changed
        self.listings: list[str] = []

    async def ensure(self, owner, spec):
        return "sandbox-1"

    def holds_copy(self, owner):
        return False

    def copy_fresh(self, owner):
        return False

    async def execute(self, owner, name, arguments, *, timeout=None):
        command = json.loads(arguments)["command"]
        if command.startswith("find /workspace"):
            self.listings.append(command)
            return json.dumps({"output": self.changed, "exit_code": 0, "error": None})
        return RAN


def terminal_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "terminal",
        ToolSchema(name="terminal", description="terminal",
                   parameters={"type": "object", "properties": {"command": {"type": "string"}}}),
        handler=AsyncMock(return_value=RAN),
    )
    return registry


def chat(channel: str = "web", **config) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), parent_id=None, agent_id="test-agent", model="test-model", channel=channel,
        config={"storage_bucket": "b", **config},
    )


async def run(session, command: str, *, pod=None, session_factory=None) -> str:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=lambda *a, **k: next(_ids))
    store.advance_harness_cursor = AsyncMock()
    result = await execute_single_tool(
        {"id": "call_1", "function": {"name": "terminal", "arguments": json.dumps({"command": command})}},
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=terminal_registry(),
        tenant=MagicMock(asset_root="/tmp/test", sandbox_spec=None),
        sandbox_pool=pod,
        session_factory=session_factory,
        redis=MagicMock(),
    )
    return result["content"]


async def test_a_script_that_leaves_a_document_behind_is_told():
    pod = Pod("/workspace/deck.pptx\n")
    before = time.time()
    said = json.loads(await run(chat(), "python make_deck.py", pod=pod))
    assert said["output"] == "done"
    assert said["note"] == SCRIPTED_NOTE.format(paths="deck.pptx")
    # What changed since the command began, in a pod already made.
    (since,) = [float(re.search(r"-newermt @([\d.]+) ", command).group(1)) for command in pod.listings]
    assert before - 2 <= since <= time.time() - 2


async def test_a_script_that_leaves_many_documents_names_ten_and_counts_the_rest():
    pod = Pod("".join(f"/workspace/out/{i:02}.pdf\n" for i in range(12)))
    said = json.loads(await run(chat(), "python split.py", pod=pod))
    named = ", ".join(f"out/{i:02}.pdf" for i in range(10))
    # Splitting a PDF is the pdf skill's work: its note says which PDF work goes through office_edit and which not.
    assert said["note"] == SCRIPTED_PDF_NOTE.format(paths=f"{named} and 2 more")


async def test_a_script_that_leaves_pdfs_and_other_documents_is_told_of_each_apart():
    pod = Pod("/workspace/deck.pptx\n/workspace/merged.pdf\n/workspace/notes.DOCX\n/workspace/scan.PDF\n")
    said = json.loads(await run(chat(), "python build.py", pod=pod))
    assert said["note"] == " ".join([
        SCRIPTED_NOTE.format(paths="deck.pptx, notes.DOCX"),
        SCRIPTED_PDF_NOTE.format(paths="merged.pdf, scan.PDF"),
    ])


async def test_a_command_that_only_reads_a_document_is_left_as_it_ran():
    pod = Pod("")
    assert await run(chat(), "pdftotext a.pdf -", pod=pod) == RAN
    assert len(pod.listings) == 1


async def test_a_chat_without_the_office_tools_is_never_looked_at():
    pod = Pod("/workspace/deck.pptx\n")
    assert await run(chat("api"), "python make_deck.py", pod=pod) == RAN
    assert pod.listings == []


async def test_a_command_on_the_users_computer_is_never_looked_at():
    pod = Pod("/workspace/deck.pptx\n")
    session = chat(execution={"kind": "device", "device_id": str(uuid4())}, workspace_path="/home/me/notes")
    assert await run(session, "python make_deck.py", pod=pod, session_factory=MagicMock()) == RAN
    assert pod.listings == []


def holding(path: str):
    async def open_locations(session_factory, locations):
        return {location for location in locations if location.endswith(f"/{path}")}
    return open_locations


async def test_a_command_naming_a_document_open_in_the_editor_gets_what_a_read_of_it_gets(monkeypatch):
    monkeypatch.setattr(office_sessions, "open_locations", holding("docs/a.docx"))
    session, factory = chat(), MagicMock()
    ran = await run(session, "pandoc /workspace/docs/a.docx -t plain", pod=Pod(""), session_factory=factory)
    said = json.loads(ran)
    assert said["note"] == await guard.open_document_note({"path": "docs/a.docx"}, session, session_factory=factory)
    assert said["note"].startswith("docs/a.docx is open in the document editor")


async def test_a_command_whose_open_documents_cannot_be_told_is_answered_as_it_ran(monkeypatch):
    monkeypatch.setattr(office_sessions, "open_locations", AsyncMock(side_effect=ConnectionError("database away")))
    assert await run(chat(), "pandoc docs/a.docx -t plain", pod=Pod(""), session_factory=MagicMock()) == RAN


async def test_a_note_that_cannot_be_made_leaves_the_result_as_it_ran(monkeypatch):
    def unreadable():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(skill_visibility, "load_settings", unreadable)
    assert await run(chat(), "python make_deck.py", pod=Pod("/workspace/deck.pptx\n")) == RAN


async def test_a_script_that_changed_a_document_open_in_the_editor_is_told_the_editor_holds_it(monkeypatch):
    # The editor is its only writer: what the script did there is not the document.
    monkeypatch.setattr(office_sessions, "open_locations", holding("docs/a.docx"))
    session, factory = chat(), MagicMock()
    pod = Pod("/workspace/docs/a.docx\n/workspace/deck.pptx\n")
    said = json.loads(await run(session, "python fix.py", pod=pod, session_factory=factory))
    assert said["note"] == " ".join([
        await guard.open_document_note({"path": "docs/a.docx"}, session, session_factory=factory),
        SCRIPTED_NOTE.format(paths="deck.pptx"),
    ])
