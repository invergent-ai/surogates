"""A project thread's row, derived from its facts as the shell's ThreadRow.

The fixtures are the shell's own (``web/src/lib/projects.ts``): a thread in
every group and with every reason, built here from the facts the store reads,
so that the routes and the desktop draw the same rows.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.workstreams.derive import GROUPS, REASONS, SHELL_LIMITS, ThreadFacts, derive_thread, question_of

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CONTRACT = Path(__file__).resolve().parents[1] / "web/src/lib/projects-contract.d.ts"
CLOUD = {"kind": "cloud"}


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def wire(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def event(event_id: int, type_: str, **data) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, type=type_, data=data)


def item(kind: str, status: str, title: str, source_event_id: int) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, status=status, title=title, source_event_id=source_event_id)


def summary(event_id: int, recap: str, *files: tuple[str, str, str]) -> SimpleNamespace:
    return event(event_id, "turn.summary", recap=recap, artifacts=[
        {"kind": kind, "label": label, "ref": ref} for kind, label, ref in files
    ])


def todos(event_id: int, done: int, total: int) -> SimpleNamespace:
    return event(event_id, "todo.updated", todos=[
        {"id": str(i), "content": f"Step {i}", "status": "completed" if i < done else "pending"}
        for i in range(total)
    ] + [{"id": "x", "content": "Dropped", "status": "cancelled"}])


def facts(thread_id: str, title: str, minutes: float, status: str, *, events=(), items=(),
          resolved_at=None, place=CLOUD) -> ThreadFacts:
    # sessions.updated_at is naive UTC; the row's times are aware.
    return ThreadFacts(
        id=UUID(thread_id), workstream_id=UUID(REPORT), title=title, status=status,
        created_at=ago(minutes + 60), updated_at=ago(minutes).replace(tzinfo=None),
        resolved_at=resolved_at, place=place, items=tuple(items), events=tuple(events),
    )


def row(thread_id: str, title: str, minutes: float, *, group: str, reason=None, status_line=None,
        progress=None, files=(), place=CLOUD, resolved_at=None) -> dict:
    return {
        "id": thread_id, "title": title, "group": group, "reason": reason,
        "status_line": status_line, "progress": progress,
        "files": [{"kind": kind, "label": label, "ref": ref, "thread_id": thread_id} for kind, label, ref in files],
        "place": place, "created_at": wire(ago(minutes + 60)), "updated_at": wire(ago(minutes)),
        "resolved_at": resolved_at and wire(resolved_at),
    }


# The shell's FIXTURE_IDS.
REPORT = "0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f"
QUESTION = "4fac7a5c-ce6b-4a9c-9d54-5e6f708192a3"
APPROVAL = "5abd8b6d-df7c-4bad-8e65-6f708192a3b4"
FAILED = "6bce9c7e-e08d-4cbe-9f76-708192a3b4c5"
WORKING = "2d8a5e3a-ac4f-4e7a-9b32-3c4d5e6f7081"
COMPUTER = "7cdfad8f-f19e-4dcf-8a87-8192a3b4c5d6"
IDLE = "3e9b6f4b-bd5a-4f8b-8c43-4d5e6f708192"
RESOLVED = "8de0be90-02af-4ed0-9b98-92a3b4c5d6e7"
THINKPAD = {"kind": "device", "device_id": "d", "device_name": "thinkpad", "online": False}
REVENUE = ("file", "revenue.xlsx", "threads/revenue/revenue.xlsx")

SHELL_FIXTURES = {
    "question": (
        facts(QUESTION, "Check the revenue figures", 17, "active", events=[
            event(1, "user.message", content="Check the revenue figures."),
            summary(3, "Loaded the ledger.", REVENUE),
            todos(5, 2, 5),
        ], items=[item("input_required", "pending", "Which quarter's exchange rate should I use?", 9)]),
        row(QUESTION, "Check the revenue figures", 17, group="waiting", reason="question",
            status_line="Which quarter's exchange rate should I use?", progress={"done": 2, "total": 5},
            files=[REVENUE]),
    ),
    "approval": (
        facts(APPROVAL, "Send the draft to finance", 25, "active", events=[
            event(1, "user.message", content="Send the draft to finance."),
        ], items=[item("action_required", "pending", "Send an email to finance@example.com?", 4)]),
        row(APPROVAL, "Send the draft to finance", 25, group="waiting", reason="approval",
            status_line="Send an email to finance@example.com?"),
    ),
    "failed": (
        facts(FAILED, "Convert the old reports", 60, "failed", events=[
            event(1, "user.message", content="Convert the old reports."),
            event(6, "session.fail", reason="llm_error", error="The PDF could not be opened: it is encrypted"),
        ]),
        row(FAILED, "Convert the old reports", 60, group="waiting", reason="failed",
            status_line="The PDF could not be opened: it is encrypted"),
    ),
    "working": (
        facts(WORKING, "Draft the summary", 28, "active", events=[
            summary(8, "Drafted the introduction.", ("file", "summary.docx", "threads/summary/summary.docx"),
                    ("artifact", "Sales chart", "art-1")),
            event(10, "user.message", content="Now write the outlook."),
            event(12, "iteration.summary", summary="Writing the outlook"),
            todos(13, 3, 6),
        ]),
        row(WORKING, "Draft the summary", 28, group="working", status_line="Writing the outlook",
            progress={"done": 3, "total": 6},
            files=[("file", "summary.docx", "threads/summary/summary.docx"), ("artifact", "Sales chart", "art-1")]),
    ),
    "computer": (
        facts(COMPUTER, "Tidy the shared folder", 40, "active", place=THINKPAD, events=[
            event(10, "user.message", content="Tidy the shared folder."),
            event(11, "harness.wake"),
            event(12, "iteration.summary", summary="Listing the folder"),
            event(14, "device.waiting", device_id="d", device_name="thinkpad", reason="offline"),
        ]),
        row(COMPUTER, "Tidy the shared folder", 40, group="working", reason="computer",
            status_line="Waiting for thinkpad", place=THINKPAD),
    ),
    "idle": (
        facts(IDLE, "Collect the sales data", 540, "completed", events=[
            event(19, "llm.response", message={"role": "assistant", "content": "All four regions are in."}),
            summary(20, "Done: 4 regions", *[("file", f"{r}.csv", f"threads/sales/{r}.csv") for r in ("north", "south", "east")]),
        ]),
        row(IDLE, "Collect the sales data", 540, group="idle", status_line="Done: 4 regions",
            files=[("file", f"{r}.csv", f"threads/sales/{r}.csv") for r in ("north", "south", "east")]),
    ),
    "resolved": (
        facts(RESOLVED, "Book the review meeting", 9_000, "completed", resolved_at=ago(8_900), events=[
            event(4, "llm.response", message={"role": "assistant", "content": "Booked it."}),
            summary(5, "Booked for Monday"),
        ]),
        row(RESOLVED, "Book the review meeting", 9_000, group="resolved", status_line="Booked for Monday",
            resolved_at=ago(8_900)),
    ),
}


@pytest.mark.parametrize("case", SHELL_FIXTURES)
def test_each_of_the_shells_threads_is_derived_from_its_facts(case):
    given, expected = SHELL_FIXTURES[case]
    assert derive_thread(given, now=NOW) == expected


THREAD = "9f0e1d2c-3b4a-4c5d-8e6f-708192a3b4c5"


@pytest.mark.parametrize("given, group, reason, status_line", [
    # A question that outlived ask_user_question's 30 minutes still waits for its answer.
    (facts(THREAD, "T", 60, "completed", events=[event(1, "user.message", content="Go.")],
           items=[item("input_required", "expired", "Which year?", 3)]),
     "waiting", "question", "Which year?"),
    # The answer, typed into the thread, ends the wait.
    (facts(THREAD, "T", 60, "active", events=[event(7, "user.message", content="2025.")],
           items=[item("input_required", "expired", "Which year?", 3)]),
     "working", None, None),
    # An approval that expired asks nothing any more.
    (facts(THREAD, "T", 60, "completed", items=[item("action_required", "expired", "Send it?", 3)]),
     "idle", None, None),
    # A pending item is the newest news, ahead of the failure and an older expired question.
    (facts(THREAD, "T", 60, "failed", events=[event(6, "session.fail", reason="provider_error")],
           items=[item("input_required", "expired", "Which year?", 2), item("governance_gate", "pending", "Run the macro?", 5)]),
     "waiting", "approval", "Run the macro?"),
    # A failure before an unanswered question: the failure.
    (facts(THREAD, "T", 60, "failed", events=[event(6, "session.fail", reason="provider_error")],
           items=[item("input_required", "expired", "Which year?", 2)]),
     "waiting", "failed", "provider_error"),
    # A stopped thread is idle.
    (facts(THREAD, "T", 60, "paused"), "idle", None, None),
    # Seven quiet days resolve a thread, though nobody resolved it.
    (facts(THREAD, "T", 7 * 24 * 60 + 1, "completed"), "resolved", None, None),
    # A working thread is never resolved for being quiet.
    (facts(THREAD, "T", 8 * 24 * 60, "active"), "working", None, None),
    # Resolved wins over a waiting question.
    (facts(THREAD, "T", 60, "completed", resolved_at=ago(30),
           items=[item("input_required", "pending", "Which year?", 3)]),
     "resolved", None, None),
    # An iteration summary of the turn before the follow-up says nothing of this turn.
    (facts(THREAD, "T", 60, "active", events=[
        event(4, "iteration.summary", summary="Read the brief"), event(6, "user.message", content="Also add a chart."),
    ]), "working", None, None),
    # The computer came back.
    (facts(THREAD, "T", 60, "active", events=[
        event(4, "device.waiting", device_name="thinkpad"), event(5, "device.resumed"),
    ]), "working", None, None),
    # A new worker woke the thread after the wait; it announces a wait still live again.
    (facts(THREAD, "T", 60, "active", events=[
        event(4, "device.waiting", device_name="thinkpad"), event(5, "harness.wake"),
    ]), "working", None, None),
    # A turn that ended early has no summary: its last answer is newer than the last recap.
    (facts(THREAD, "T", 60, "completed", events=[
        summary(4, "Drafted the memo."),
        event(9, "llm.response", message={"role": "assistant", "content": "\nI ran out of steps.\nThe table is half done."}),
    ]), "idle", None, "I ran out of steps."),
    # A long line is cut.
    (facts(THREAD, "T", 60, "completed", events=[
        event(9, "llm.response", message={"role": "assistant", "content": "x" * 500}),
    ]), "idle", None, "x" * 199 + "…"),
], ids=[
    "expired-question", "expired-question-answered", "expired-approval", "pending-before-failed",
    "failed-before-expired-question", "paused", "seven-quiet-days", "quiet-but-working", "resolved-first",
    "stale-iteration-summary", "computer-back", "computer-woken", "ended-early", "long-line",
])
def test_the_first_rule_that_matches_wins(given, group, reason, status_line):
    derived = derive_thread(given, now=NOW)
    assert (derived["group"], derived["reason"], derived["status_line"]) == (group, reason, status_line)


def test_a_file_named_in_two_turns_is_listed_once_newest_first():
    given = facts(THREAD, "T", 60, "completed", events=[
        summary(3, "First draft.", ("file", "A.docx", "threads/a/A.docx"), ("file", "notes.md", "threads/a/notes.md")),
        summary(8, "Second draft.", ("file", "A.docx (v2)", "threads/a/A.docx")),
    ])
    assert [(f["label"], f["ref"]) for f in derive_thread(given, now=NOW)["files"]] == [
        ("A.docx (v2)", "threads/a/A.docx"), ("notes.md", "threads/a/notes.md"),
    ]


@pytest.mark.parametrize("given, asked", [
    (facts(THREAD, "T", 60, "active", items=[
        item("input_required", "pending", "Which year?", 3), item("action_required", "pending", "Send it?", 5),
    ]), "Which year?"),
    (facts(THREAD, "T", 60, "completed", events=[event(1, "user.message", content="Go.")],
           items=[item("input_required", "expired", "Which year?", 3)]), "Which year?"),
    (facts(THREAD, "T", 60, "active", events=[event(7, "user.message", content="2025.")],
           items=[item("input_required", "expired", "Which year?", 3)]), None),
    (facts(THREAD, "T", 60, "active", items=[item("action_required", "pending", "Send it?", 5)]), None),
], ids=["pending", "expired-unanswered", "expired-answered", "an-approval-only"])
def test_the_question_a_thread_waits_on(given, asked):
    found = question_of(given)
    assert (found.title if found else None) == asked


def test_a_row_lists_its_newest_files_and_leaves_out_what_the_shell_refuses():
    # The shell refuses every row over a list longer than 200, or one entry it cannot open.
    given = facts(IDLE, "Collect the sales data", 540, "completed", events=[
        summary(1, "Started", *[("file", f"day-{i}.csv", f"threads/sales/day-{i}.csv") for i in range(150)]),
        event(2, "turn.summary", recap="Totals", artifacts=[
            {"kind": "url", "label": "Source", "ref": "https://example.com"},
            {"kind": "file", "label": "No ref"},
            {"kind": "file", "label": 7, "ref": "threads/sales/total.csv"},
            # 251 code points, but 502 UTF-16 units: the shell counts units.
            {"kind": "file", "label": "\U0001F4CA" * 251, "ref": "threads/sales/chart.png"},
            {"kind": "file", "label": "deep.csv", "ref": "threads/sales/" + "a" * 4083},
        ]),
        summary(3, "Done", *[("file", f"week-{i}.csv", f"threads/sales/week-{i}.csv") for i in range(60)]),
    ])
    files = derive_thread(given, now=NOW)["files"]
    assert len(files) == SHELL_LIMITS["files"]
    assert not {"threads/sales/chart.png", "threads/sales/" + "a" * 4083} & {f["ref"] for f in files}
    assert files[0]["ref"] == "threads/sales/week-0.csv"
    assert files[60] == {
        "kind": "file", "label": "threads/sales/total.csv", "ref": "threads/sales/total.csv", "thread_id": IDLE,
    }
    assert files[-1]["ref"] == "threads/sales/day-138.csv"


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _interface(source: str, name: str) -> str:
    return re.search(rf"interface {name}\b[^{{]*\{{(.*?)\n\}}", source, re.S).group(1)


def test_a_row_has_the_shells_fields_and_values():
    # The shell's types are the contract the routes answer in snake_case.
    source = CONTRACT.read_text()
    thread_row = _interface(source, "ThreadRow")
    fields = re.findall(r"^\s*(\w+)\??:", thread_row, re.M)
    file_fields = re.findall(r"^\s*(\w+)\??:", _interface(source, "ProducedFile"), re.M)
    groups = re.findall(r'"(\w+)"', re.search(r"type ThreadGroup = ([^;]+);", source).group(1))
    reasons = re.findall(r'"(\w+)"', re.search(r"^\s*reason: ([^;]+);", thread_row, re.M).group(1))
    assert (list(GROUPS), list(REASONS)) == (groups, reasons)
    for given, _ in SHELL_FIXTURES.values():
        derived = derive_thread(given, now=NOW)
        assert list(derived) == [_snake(field) for field in fields]
        assert derived["group"] in GROUPS and derived["reason"] in (*REASONS, None)
        assert all(list(f) == [_snake(field) for field in file_fields] for f in derived["files"])
        assert all(derived[key] is None or derived[key].endswith("Z") for key in ("created_at", "updated_at", "resolved_at"))
