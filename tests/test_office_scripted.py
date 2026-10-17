"""The office documents a script left in the workspace are found by one look in the sandbox after the call."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.office.scripted import SCRIPTED_NOTE, named_documents, scripted_documents


def pool_saying(output: str, *, holds_copy: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(return_value=json.dumps({"output": output, "exit_code": 0, "error": None})),
        holds_copy=lambda owner: holds_copy,
    )


def cloud_session(**config) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), parent_id=None, channel="web", config={"storage_bucket": "b", **config})


@pytest.mark.asyncio
async def test_the_documents_a_script_left_are_named_from_the_workspace():
    pool = pool_saying("/workspace/deck.pptx\n/workspace/sub/b.xlsx\n")
    session = cloud_session()
    assert await scripted_documents(session, 1_000.0, sandbox_pool=pool) == ["deck.pptx", "sub/b.xlsx"]
    owner, name, arguments = pool.execute.call_args.args
    assert (owner, name) == (str(session.id), "terminal")
    command = json.loads(arguments)["command"]
    assert command.startswith("find /workspace -maxdepth 6 -type f \\( -iname '*.docx' -o -iname '*.xlsx' -o ")
    assert "-iname '*.pptx' -o -iname '*.pdf' \\)" in command
    # A mounted workspace keeps whole seconds, on another machine's clock: two seconds before the call count.
    assert "-newermt @998.000 " in command
    assert "-not -path '*/.skills/*'" in command
    assert pool.execute.call_args.kwargs["timeout"] == 5


@pytest.mark.asyncio
async def test_a_threads_copy_counts_from_the_call_itself():
    # The copy is on the pod's own disk, made when the pod starts, just before the call: its times are exact.
    pool = pool_saying("", holds_copy=True)
    await scripted_documents(cloud_session(), 1_000.0, sandbox_pool=pool)
    assert "-newermt @1000.000 " in json.loads(pool.execute.call_args.args[2])["command"]


@pytest.mark.asyncio
async def test_a_helper_looks_in_the_sandbox_it_shares():
    root = str(uuid4())
    pool = pool_saying("")
    await scripted_documents(cloud_session(sandbox_root_session_id=root), 1_000.0, sandbox_pool=pool)
    assert pool.execute.call_args.args[0] == root


@pytest.mark.asyncio
async def test_nothing_changed_is_nothing_named():
    assert await scripted_documents(cloud_session(), 1_000.0, sandbox_pool=pool_saying("")) == []


@pytest.mark.asyncio
async def test_a_look_that_fails_names_nothing():
    pool = SimpleNamespace(execute=AsyncMock(side_effect=TimeoutError()), holds_copy=lambda owner: False)
    assert await scripted_documents(cloud_session(), 1_000.0, sandbox_pool=pool) == []
    pool = SimpleNamespace(execute=AsyncMock(return_value="not json"), holds_copy=lambda owner: False)
    assert await scripted_documents(cloud_session(), 1_000.0, sandbox_pool=pool) == []


@pytest.mark.asyncio
async def test_a_session_on_the_users_computer_is_never_looked_at():
    pool = pool_saying("/workspace/deck.pptx\n")
    session = cloud_session(execution={"kind": "device", "device_id": str(uuid4())}, workspace_path="/home/me/notes")
    assert await scripted_documents(session, 1_000.0, sandbox_pool=pool) == []
    pool.execute.assert_not_called()


def test_the_note_names_the_documents():
    assert SCRIPTED_NOTE.format(paths="deck.pptx") == (
        "deck.pptx changed by a script: the user can neither watch nor co-edit a document made that way. "
        "Make and change documents with office_edit (read them with office_read)."
    )


@pytest.mark.parametrize(("command", "named"), [
    ("pdftotext a.pdf -", {"a.pdf"}),
    ("python make_deck.py", set()),
    ("ls *.pdf && cat notes.pdf.bak", set()),
    ("libreoffice --convert-to pdf --outdir=out /workspace/docs/Report.DOCX", {"/workspace/docs/Report.DOCX"}),
    ("python -c \"import openpyxl; openpyxl.load_workbook('data.xlsx')\"", {"data.xlsx"}),
])
def test_the_documents_a_command_names(command, named):
    assert named_documents(command) == named


def test_a_quoted_name_with_spaces_is_named_whole():
    assert "Q3 Report.pdf" in named_documents('pdftotext "Q3 Report.pdf" -')
