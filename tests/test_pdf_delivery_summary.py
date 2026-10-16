"""A generated PDF survives inspection and Python's incidental cache files."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.session.events import EventType


async def test_pdf_summary_keeps_inspected_pdf_and_excludes_generator_and_cache():
    session_id = uuid4()
    turn_id = str(uuid4())
    started = datetime(2026, 9, 10, 9, 43, 54, tzinfo=timezone.utc)
    modified = datetime(2026, 9, 10, 9, 44, 54, tzinfo=timezone.utc)
    pdf = "stirile-zilei-hotnews-2026-09-10.pdf"
    script = "make_stiri_pdf.py"
    events = [
        SimpleNamespace(type=EventType.LLM_REQUEST, data={"turn_id": turn_id}),
        SimpleNamespace(type=EventType.TOOL_CALL, data={
            "name": "write_file", "arguments": {"path": script},
        }),
        SimpleNamespace(type=EventType.TOOL_CALL, data={
            "name": "terminal", "arguments": {"command": (
                f"python3 {script} && pdfinfo {pdf} 2>/dev/null | head -12; "
                f'pdftotext -layout {pdf} - | grep -n "Diverse" -A3'
            )},
        }),
        SimpleNamespace(type=EventType.TOOL_CALL, data={
            "name": "terminal", "arguments": {"command": (
                f"pdfinfo {pdf} | grep -i pages; "
                f"pdftoppm -png -r 70 {pdf} preview && ls preview*"
            )},
        }),
    ]
    session = SimpleNamespace(
        id=session_id, channel="studio", config={"storage_bucket": "workspace"},
    )
    harness = ArtifactCompletionMixin()
    harness._turn_started_at = started
    harness._pending_iteration_summary_tasks = {}
    harness._turn_summarizer = None
    harness._store = AsyncMock()
    harness._store.get_session.return_value = session
    harness._store.get_events.return_value = events
    harness._storage = AsyncMock()
    harness._storage.list_entries.return_value = [
        {"key": f"{session_id}/{path}", "size": size, "modified": modified}
        for path, size in [
            (script, 6785),
            (pdf, 68847),
            ("__pycache__/make_stiri_pdf.cpython-312.pyc", 7100),
        ]
    ]

    await harness._drain_and_emit_turn_summary(
        session_id=session_id, turn_id=turn_id,
        user_message="scrie-le intr-un pdf",
        final_message=f"Gata. Am făcut PDF-ul: {pdf}",
    )

    harness._store.emit_event.assert_awaited_once_with(
        session_id, EventType.TURN_SUMMARY, {
            "turn_id": turn_id,
            "recap": "",
            "rejected": [{"ref": script, "reason": "scaffolding"}],
            "artifacts": [{"kind": "file", "label": pdf, "ref": pdf}],
        },
    )


async def test_a_turn_whose_folder_could_not_be_listed_flags_no_file_its_reply_names(monkeypatch):
    session_id, turn_id = uuid4(), str(uuid4())
    session = SimpleNamespace(id=session_id, channel="web", config={
        "execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/me/notes",
    })
    events = [
        SimpleNamespace(type=EventType.LLM_REQUEST, data={"turn_id": turn_id}),
        SimpleNamespace(type=EventType.TOOL_CALL, data={"name": "terminal", "arguments": {"command": "make report.pdf"}}),
    ]

    async def no_walk(self, session, *, since):
        return None  # its computer did not answer in time

    monkeypatch.setattr(ArtifactCompletionMixin, "_walk_folder", no_walk)
    # Its computer did not say where the turn began, or did and then did not list it.
    for cursor in (None, "1700000000000000000"):
        harness = ArtifactCompletionMixin()
        harness._turn_started_at = datetime.now(timezone.utc)
        harness._turn_marked, harness._turn_cursor = True, cursor  # it made a tool call
        harness._pending_iteration_summary_tasks = {}
        harness._turn_summarizer = None
        harness._storage = None
        harness._store = AsyncMock()
        harness._store.get_session.return_value = session
        harness._store.get_events.return_value = events

        await harness._drain_and_emit_turn_summary(
            session_id=session_id, turn_id=turn_id, user_message="make the report",
            final_message="I saved report.pdf in the folder.",
        )

        # Unseen is not unsupported: a wrong warning is worse than none.
        harness._store.emit_event.assert_not_awaited()


STARTED = datetime(2026, 10, 16, 9, 0, 0, tzinfo=timezone.utc)
REPORT = {"kind": "file", "label": "report.md", "ref": "report.md"}


def call(call_id, tool, args):
    return SimpleNamespace(type=EventType.TOOL_CALL, data={"name": tool, "arguments": args, "tool_call_id": call_id})


def result(call_id, tool, content):
    return SimpleNamespace(type=EventType.TOOL_RESULT, data={
        "name": tool, "content": content, "elapsed_ms": 1, "tool_call_id": call_id,
    })


async def card(events, listed=()):
    """The files the turn's summary names, after an earlier turn's *events* and this one's, ending with report.md written.

    *events* is a list of an earlier turn's events and a list of this turn's; *listed* is the workspace's other files,
    each a (path, size, written in this turn).
    """
    session_id, turn_id = uuid4(), str(uuid4())
    earlier, this_turn = events
    log = [
        *([SimpleNamespace(type=EventType.LLM_REQUEST, data={"turn_id": "earlier"}), *earlier] if earlier else []),
        SimpleNamespace(type=EventType.LLM_REQUEST, data={"turn_id": turn_id}),
        *this_turn,
        call("c9", "write_file", {"path": "report.md", "content": "# Report\n"}),
        result("c9", "write_file", json.dumps({"status": "ok", "path": "report.md", "bytes_written": 9, "lines_written": 1})),
    ]
    for event_id, event in enumerate(log, 1):
        event.id = event_id

    async def get_events(session_id, *, types=None, after=None, **_):
        return [e for e in log if (types is None or e.type in types) and (after is None or e.id > after)]

    harness = ArtifactCompletionMixin()
    harness._turn_started_at = STARTED
    harness._pending_iteration_summary_tasks = {}
    harness._turn_summarizer = None
    harness._store = AsyncMock()
    harness._store.get_session.return_value = SimpleNamespace(
        id=session_id, channel="web", config={"storage_bucket": "workspace"},
    )
    harness._store.get_events.side_effect = get_events
    harness._storage = AsyncMock()
    harness._storage.list_entries.return_value = [
        {"key": f"{session_id}/{path}", "size": size, "modified": STARTED + timedelta(minutes=1 if new else -60)}
        for path, size, new in [*listed, ("report.md", 9, True)]
    ]

    await harness._drain_and_emit_turn_summary(
        session_id=session_id, turn_id=turn_id, user_message="write the report", final_message="Done.",
    )

    harness._store.emit_event.assert_awaited_once()
    (_, kind, summary), _ = harness._store.emit_event.await_args
    assert kind == EventType.TURN_SUMMARY
    assert summary["turn_id"] == turn_id and "rejected" not in summary
    return summary["artifacts"]


@pytest.mark.parametrize("tool, args, content, listed", [
    # The editor's guard refuses a text write to a document; nothing was there to list.
    ("write_file", {"path": "sample.docx", "content": "hello"}, json.dumps({"error": (
        "sample.docx is a Word document: change it with office_edit (read it with office_read), not with write_file."
    )}), []),
    # patch follows a text it could not find with a hint, after the JSON: the file is there, unchanged.
    ("patch", {"path": "notes.md", "old_string": "a", "new_string": "b"}, json.dumps({
        "error": "Could not find the specified text in notes.md",
    }) + "\n\n[Hint: old_string not found. Use read_file to verify the current content.]", [("notes.md", 12, False)]),
    # A V4A patch whose validation failed changed none of its files.
    ("patch", {"mode": "patch", "path": "notes.md", "patch": "*** Begin Patch\n*** Update File: notes.md\n-a\n+b\n*** End Patch"},
     json.dumps({"status": "error", "files": [], "errors": [
         "Patch validation failed (no files were modified):", "notes.md: context not found",
     ]}), [("notes.md", 12, False)]),
])
async def test_a_refused_write_is_no_delivered_file(tool, args, content, listed):
    assert await card(([], [call("c1", tool, args), result("c1", tool, content)]), listed) == [REPORT]


@pytest.mark.parametrize("results", [
    [],  # its result is not written yet
    [result("c1", "write_file", json.dumps({"status": "ok", "path": "draft.md", "error": None}))],
    [result("c1", "write_file", json.dumps({"status": "ok", "path": "draft.md", "error": False}))],
])
async def test_a_write_not_refused_is_delivered(results):
    this_turn = [call("c1", "write_file", {"path": "draft.md", "content": "draft"}), *results]
    draft = {"kind": "file", "label": "draft.md", "ref": "draft.md"}
    # Unlisted, as a just-written file can be: only its call names it.
    assert await card(([], this_turn)) == [draft, REPORT]


async def test_an_earlier_turns_refusal_drops_no_call_of_this_turn():
    # Some providers number their calls from call_0 in every response.
    refused = result("call_0", "write_file", json.dumps({"error": "Refusing to overwrite 'draft.md'"}))
    earlier = [call("call_0", "write_file", {"path": "draft.md", "content": "a"}), refused]
    this_turn = [call("call_0", "write_file", {"path": "draft.md", "content": "b"})]
    draft = {"kind": "file", "label": "draft.md", "ref": "draft.md"}
    assert await card((earlier, this_turn)) == [draft, REPORT]


async def test_an_earlier_turns_write_keeps_no_refused_call_of_this_turn():
    landed = result("call_0", "write_file", json.dumps({"status": "ok", "path": "draft.md"}))
    earlier = [call("call_0", "write_file", {"path": "draft.md", "content": "a"}), landed]
    this_turn = [
        call("call_0", "write_file", {"path": "draft.md", "content": "b"}),
        result("call_0", "write_file", json.dumps({"error": "Refusing to overwrite 'draft.md'"})),
    ]
    assert await card((earlier, this_turn)) == [REPORT]
