"""A project thread's row, derived from its facts as the shell's ThreadRow.

The fixtures are the shell's own (``web/src/lib/projects.ts``): a thread in
every group and with every reason, built here from the facts the store reads,
so that the routes and the desktop draw the same rows.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.workstreams.derive import (
    GROUPS, LATEST_TYPES, REASONS, SHELL_LIMITS, ThreadFacts, derive_thread, question_of, set_aside_name, undone_files,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CONTRACT = Path(__file__).resolve().parents[1] / "web/src/lib/projects-contract.d.ts"
CLOUD = {"kind": "cloud"}


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def wire(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def event(event_id: int, type_: str, **data) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, type=type_, data=data)


def item(kind: str, status: str, title: str, source_event_id: int, **payload) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, status=status, title=title, source_event_id=source_event_id, payload=payload)


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
        id=UUID(thread_id), title=title, status=status,
        created_at=ago(minutes + 60), updated_at=ago(minutes).replace(tzinfo=None),
        resolved_at=resolved_at, place=place, items=tuple(items), events=tuple(events),
    )


def row(thread_id: str, title: str, minutes: float, *, group: str, reason=None, status_line=None,
        progress=None, files=(), place=CLOUD, resolved_at=None) -> dict:
    return {
        "id": thread_id, "title": title, "group": group, "reason": reason,
        "status_line": status_line, "progress": progress,
        # A thread with no landing: its files are its turn summaries', with no mark.
        "files": [{"kind": kind, "label": label, "ref": ref, "thread_id": thread_id, "landing": None} for kind, label, ref in files],
        "more_files": 0,
        "landing_id": None,
        "set_aside": None,
        "place": place, "created_at": wire(ago(minutes + 60)), "updated_at": wire(ago(minutes)),
        "resolved_at": resolved_at and wire(resolved_at),
    }


# The shell's FIXTURE_IDS.
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
    # A landing left half done in its folder on a computer, which no worker runs, waits on you, though its thread works.
    (replace(facts(THREAD, "T", 60, "active", place=THINKPAD), half_landed=True),
     "waiting", "files", "Couldn't finish landing my changes"),
    # A pending item, and a failure, come before it; it comes before a question that expired.
    (replace(facts(THREAD, "T", 60, "active", place=THINKPAD, items=[item("action_required", "pending", "Send it?", 3)]),
             half_landed=True),
     "waiting", "approval", "Send it?"),
    (replace(facts(THREAD, "T", 60, "failed", place=THINKPAD, events=[event(6, "session.fail", reason="provider_error")]),
             half_landed=True),
     "waiting", "failed", "provider_error"),
    (replace(facts(THREAD, "T", 60, "completed", place=THINKPAD, events=[event(1, "user.message", content="Go.")],
                   items=[item("input_required", "expired", "Which year?", 3)]), half_landed=True),
     "waiting", "files", "Couldn't finish landing my changes"),
], ids=[
    "expired-question", "expired-question-answered", "expired-approval", "pending-before-failed",
    "failed-before-expired-question", "paused", "seven-quiet-days", "quiet-but-working", "resolved-first",
    "stale-iteration-summary", "computer-back", "computer-woken", "ended-early", "long-line",
    "half-landed", "pending-before-half-landed", "failed-before-half-landed", "half-landed-before-expired-question",
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
    # The coordinator's follow-up is a message after it too, though not the user's.
    (facts(THREAD, "T", 60, "active", events=[
        event(1, "user.message", content="Go."), event(7, "coordinator.message", content="Use 2025."),
    ], items=[item("input_required", "expired", "Which year?", 3)]), None),
    (facts(THREAD, "T", 60, "active", items=[item("action_required", "pending", "Send it?", 5)]), None),
], ids=["pending", "expired-unanswered", "expired-answered", "expired-followed-up", "an-approval-only"])
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
        "kind": "file", "label": "threads/sales/total.csv", "ref": "threads/sales/total.csv", "thread_id": IDLE, "landing": None,
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
    aside_fields = re.findall(r"^\s*(\w+)\??:", _interface(source, "SetAside"), re.M)
    whole_fields = re.findall(r"^\s*(\w+)\??:", _interface(source, "SetAsideWhole"), re.M)
    groups = re.findall(r'"(\w+)"', re.search(r"type ThreadGroup = ([^;]+);", source).group(1))
    reasons = re.findall(r'"(\w+)"', re.search(r"^\s*reason: ([^;]+);", thread_row, re.M).group(1))
    assert (list(GROUPS), list(REASONS)) == (groups, reasons)
    for given, _ in SHELL_FIXTURES.values():
        derived = derive_thread(given, now=NOW)
        assert list(derived) == [_snake(field) for field in fields]
        assert derived["group"] in GROUPS and derived["reason"] in (*REASONS, None)
        assert all(list(f) == [_snake(field) for field in file_fields] for f in derived["files"])
        assert all(derived[key] is None or derived[key].endswith("Z") for key in ("created_at", "updated_at", "resolved_at"))
    # What a thread's computer set aside, where it set any aside.
    aside = derive_thread(replace(SHELL_FIXTURES["computer"][0], set_aside=SET_ASIDE_TOLD), now=NOW)["set_aside"]
    assert list(aside) == [_snake(field) for field in aside_fields]
    assert aside["kept"] and aside["gone"] and all(list(whole) == whole_fields for whole in (*aside["kept"], *aside["gone"]))
    # Each of the kinds the shell takes, and none other.
    kinds = re.findall(r'"(\w+)"', re.search(r"^\s*kind: ([^;]+);", _interface(source, "SetAsideWhole"), re.M).group(1))
    every = {"copies": [REPOSITORY_NAME, COPY_NAME], "snapshots": [], "gone": [], "places": [PLACE_NAME]}
    placed = derive_thread(replace(SHELL_FIXTURES["computer"][0], set_aside=every), now=NOW)
    assert sorted({whole["kind"] for whole in placed["set_aside"]["kept"]}) == sorted(kinds) == ["copy", "place", "repository"]


def test_a_thread_whose_files_did_not_merge_waits_on_you_with_the_reason_files():
    given = facts(IDLE, "Draft A", 5, "completed", events=[event(1, "user.message", content="Edit the report.")], items=[
        item("action_required", "pending", "Couldn't merge my changes to Report.docx", 7, action_type="files"),
    ])
    derived = derive_thread(given, now=NOW)
    assert (derived["group"], derived["reason"], derived["status_line"]) == (
        "waiting", "files", "Couldn't merge my changes to Report.docx",
    )


def landing(row_id: int, *files: tuple[str, str | None, str | None, bool]) -> dict:
    """A landing's row as the store reads it: each file ``(path, before, after, merged)``."""
    return {"id": row_id, "files": [{"path": p, "before": b, "after": a, "merged": m} for p, b, a, m in files]}


def marks(given: ThreadFacts) -> list[tuple[str, str | None]]:
    return [(f["ref"], f["landing"]) for f in derive_thread(given, now=NOW)["files"]]


def test_a_threads_files_are_what_its_landings_changed_each_with_its_mark():
    given = replace(facts(IDLE, "Draft A", 5, "completed", events=[
        summary(8, "Done.", ("artifact", "Sales chart", "art-1"), ("file", "old.md", "old.md")),
    ]), landings=(
        landing(40, ("report.docx", None, "b1", True), ("plan.md", None, "p1", True), ("budget.xlsx", None, "c1", True)),
        landing(41, ("report.docx", "b1", "b2", True), ("budget.xlsx", "c1", "c2", False),
                ("notes.md", "n1", "n2", False), ("gone.md", "g1", None, True), ("kept.md", "k1", None, False)),
    ), redoing=frozenset({"budget.xlsx", "report.docx"}))
    # What did not land comes first, where a row and a card show it.  A file is marked by its newest
    # landing, so one that landed is not being redone whatever a redo names.  A deletion that landed
    # is no file to open; one left out is still there.  A summary's files are a thread's that works
    # on the real files: here only its artifact is taken.
    assert marks(given) == [
        ("budget.xlsx", "redoing"), ("notes.md", "not_merged"), ("kept.md", "not_merged"),
        ("report.docx", "landed"), ("plan.md", "landed"), ("art-1", None),
    ]
    assert derive_thread(given, now=NOW)["files"][0] == {
        "kind": "file", "label": "budget.xlsx", "ref": "budget.xlsx", "thread_id": IDLE, "landing": "redoing",
    }
    # No redo waits for the thread's next turn: the file its landing left out is not merged.
    assert marks(replace(given, redoing=frozenset()))[0] == ("budget.xlsx", "not_merged")


def test_a_file_that_landed_since_is_not_marked_not_merged():
    given = replace(facts(IDLE, "Draft A", 5, "completed"), landings=(
        landing(42, ("budget.xlsx", "c1", "c3", True)),
        landing(41, ("budget.xlsx", "c1", "c2", False), ("notes.md", "n1", "n2", False)),
    ), redoing=frozenset({"budget.xlsx"}))
    assert marks(given) == [("notes.md", "not_merged"), ("budget.xlsx", "landed")]


@pytest.mark.parametrize("ended", [
    {"resolved_at": ago(1)},
    # Quiet for longer than a week: resolved by the quiet rule.
    {"minutes": 8 * 24 * 60},
], ids=["resolved", "quiet"])
def test_a_resolved_threads_file_is_not_being_redone(ended):
    given = replace(
        facts(IDLE, "Draft A", ended.get("minutes", 5), "completed", resolved_at=ended.get("resolved_at")),
        landings=(landing(41, ("budget.xlsx", "c1", "c2", False)),), redoing=frozenset({"budget.xlsx"}),
    )
    derived = derive_thread(given, now=NOW)
    assert (derived["group"], marks(given)) == ("resolved", [("budget.xlsx", "not_merged")])


def test_a_landing_of_two_thousand_files_lists_the_shells_limit_those_left_out_first():
    changed = [(f"data/part-{i:04}.csv", None, f"v{i}", i % 500 != 499) for i in range(2000)]
    given = replace(facts(IDLE, "Draft A", 5, "completed", events=[
        summary(8, "Done.", ("artifact", "Sales chart", "art-1")),
    ]), landings=(landing(41, *changed),))
    derived = derive_thread(given, now=NOW)
    files = derived["files"]
    assert len(files) == SHELL_LIMITS["files"]
    assert [f["ref"] for f in files[:5]] == [
        "data/part-0499.csv", "data/part-0999.csv", "data/part-1499.csv", "data/part-1999.csv", "data/part-0000.csv",
    ]
    assert [f["landing"] for f in files[:5]] == ["not_merged"] * 4 + ["landed"]
    # The row says how many more it changed, and the artifact its summary named.
    assert derived["more_files"] == 2001 - SHELL_LIMITS["files"]


def test_a_row_counts_the_files_its_turn_summaries_named_past_the_shells_limit():
    named = [("file", f"part-{i:03}.csv", f"data/part-{i:03}.csv") for i in range(250)]
    given = facts(IDLE, "Draft A", 5, "completed", events=[summary(8, "Done.", *named[:150]), summary(9, "Done.", *named[100:])])
    derived = derive_thread(given, now=NOW)
    # Each file once, however many summaries named it.
    assert (len(derived["files"]), derived["more_files"]) == (SHELL_LIMITS["files"], 50)
    assert derive_thread(facts(IDLE, "Draft A", 5, "completed", events=[summary(8, "Done.", *named[:3])]), now=NOW)["more_files"] == 0
    # A file a summary named in words the shell would refuse is no more one, where an earlier summary named it in its own.
    worded = summary(9, "Done.", ("file", "w" * (SHELL_LIMITS["label"] + 1), "data/part-000.csv"))
    derived = derive_thread(facts(IDLE, "Draft A", 5, "completed", events=[summary(8, "Done.", *named[:3]), worded]), now=NOW)
    assert ([f["label"] for f in derived["files"]], derived["more_files"]) == (["part-000.csv", "part-001.csv", "part-002.csv"], 0)


def test_a_landed_file_the_shell_would_refuse_is_left_out_and_a_name_is_sent_as_it_is():
    markup = '<img src=x onerror="alert(1)">.md'
    longest = "a" * SHELL_LIMITS["label"]
    given = replace(facts(IDLE, "Draft A", 5, "completed"), landings=(
        landing(41, (longest + "a", None, "v1", True), (markup, None, "v2", False), (longest, None, "v3", True)),
    ))
    derived = derive_thread(given, now=NOW)
    assert derived["files"] == [
        {"kind": "file", "label": markup, "ref": markup, "thread_id": IDLE, "landing": "not_merged"},
        {"kind": "file", "label": longest, "ref": longest, "thread_id": IDLE, "landing": "landed"},
    ]
    # One it cannot name is one more file it changed all the same.
    assert derived["more_files"] == 1
    # As is a file a summary named that the shell would refuse.  A deletion that landed is no file, and a thread
    # with a landing takes none of its summaries' files.
    too_long = summary(9, "Done.", ("file", "notes.md", "n" * (SHELL_LIMITS["ref"] + 1)), ("artifact", "Chart", "art-1"))
    assert derive_thread(replace(facts(IDLE, "Draft A", 5, "completed"), events=(too_long,)), now=NOW)["more_files"] == 1
    gone = landing(42, ("gone.md", "g1", None, True))
    assert derive_thread(replace(given, landings=(gone,), events=(too_long,)), now=NOW)["more_files"] == 0


def test_a_file_an_undo_put_back_is_undone_and_the_card_undoes_the_newest_landing_still_as_it_landed():
    given = replace(facts(IDLE, "Draft A", 5, "completed"), landings=(
        landing(40, ("plan.md", None, "p1", True), ("gone.md", "g1", None, True)),
        landing(41, ("report.docx", "b1", "b2", True), ("notes.md", "n1", "n2", False)),
    ), undone=frozenset({(41, "report.docx")}))
    assert marks(given) == [("notes.md", "not_merged"), ("report.docx", "undone"), ("plan.md", "landed")]
    # The newest landing has nothing left as it landed: what it left out is no change of its. The one
    # before it has, its deletion among them, which an Undo brings back.
    assert derive_thread(given, now=NOW)["landing_id"] == "40"
    assert derive_thread(replace(given, undone=frozenset()), now=NOW)["landing_id"] == "41"
    assert derive_thread(replace(given, undone=given.undone | {(40, "plan.md")}), now=NOW)["landing_id"] == "40"


def test_a_thread_with_every_landing_undone_or_none_has_none_for_its_card_to_undo():
    landed = landing(40, ("plan.md", None, "p1", True), ("gone.md", "g1", None, True))
    given = replace(facts(IDLE, "Draft A", 5, "completed"), landings=(landed,), undone=frozenset({(40, "plan.md"), (40, "gone.md")}))
    assert derive_thread(given, now=NOW)["landing_id"] is None
    # A landing that left every file out changed none of them.
    assert derive_thread(replace(given, landings=(landing(41, ("notes.md", "n1", "n2", False)),)), now=NOW)["landing_id"] is None
    assert derive_thread(facts(IDLE, "Draft A", 5, "completed"), now=NOW)["landing_id"] is None


def test_a_threads_landing_on_a_computer_marks_its_files_and_is_none_for_its_card_to_undo():
    landed = {**landing(40, ("plan.md", None, "p1", True), ("notes.md", "n1", "n2", False)), "on_computer": True}
    given = replace(facts(IDLE, "Draft A", 5, "completed"), landings=(landed,), redoing=frozenset({"notes.md"}))
    assert marks(given) == [("notes.md", "redoing"), ("plan.md", "landed")]
    assert derive_thread(given, now=NOW)["landing_id"] is None
    # An Undo is of the cloud's files: one that names the row marks none of a computer's.
    assert marks(replace(given, undone=frozenset({(40, "plan.md")}))) == [("notes.md", "redoing"), ("plan.md", "landed")]
    # The same landing in the cloud is its card's to undo.
    assert derive_thread(replace(given, landings=({**landed, "on_computer": False},)), now=NOW)["landing_id"] == "40"


def undo_row(row_id: int, undoes: list[int], *paths: str) -> dict:
    """An Undo's row as the store reads it: each file it put back, over the rows *undoes* each."""
    return {"id": row_id, "files": [{"path": p, "before": "x", "after": "y", "merged": True, "undoes": undoes} for p in paths]}


def test_an_undo_of_an_undo_brings_back_what_that_one_put_back_and_the_first_undo_is_undone_itself():
    first, second = undo_row(50, [40, 41], "plan.md"), undo_row(51, [50], "plan.md")
    assert undone_files([first]) == {(40, "plan.md"), (41, "plan.md")}
    assert undone_files([second, first]) == {(50, "plan.md")}
    # A third undoes the second: the first stands again, and what it put back is undone again.
    assert undone_files([first, second, undo_row(52, [51], "plan.md")]) == {(51, "plan.md"), (40, "plan.md"), (41, "plan.md")}


def test_an_undo_marks_each_file_undone_only_in_the_landings_it_went_back_over_at_that_file():
    # Landing 40 changed a.md and b.md, and 41 changed b.md after the Undo 50 had put back 40's b.md alone.  The
    # thread's Undo 60 then put a.md back over 40, and b.md over 41: 40's b.md was put back already.
    first = {"id": 50, "files": [{"path": "b.md", "before": "b1", "after": "b0", "merged": True, "undoes": [40]}]}
    thread = {"id": 60, "files": [
        {"path": "a.md", "before": "a1", "after": "a0", "merged": True, "undoes": [40]},
        {"path": "b.md", "before": "b2", "after": "b0", "merged": True, "undoes": [41]},
    ]}
    assert undone_files([first, thread]) == {(40, "a.md"), (40, "b.md"), (41, "b.md")}
    # An Undo of the first brings 40's b.md back: no Undo that went back over it stands.
    again = {"id": 70, "files": [{"path": "b.md", "before": "b0", "after": "b1", "merged": True, "undoes": [50]}]}
    assert undone_files([first, thread, again]) == {(50, "b.md"), (40, "a.md"), (41, "b.md")}


def test_an_undo_of_an_undo_brings_back_only_the_files_it_put_back():
    # The second Undo left b.md as it was, changed since: what the first put back of it stays put back.
    first, second = undo_row(50, [40], "a.md", "b.md"), undo_row(51, [50], "a.md")
    assert undone_files([first, second]) == {(50, "a.md"), (40, "b.md")}
    assert undone_files([]) == frozenset()


#: The names its computer gives what it set aside whole of a thread's: the order, when, whose, and which.
COPY_NAME = f"00000001-20261010T101500Z-{COMPUTER}.copy"
REPOSITORY_NAME = f"00000002-20261010T101501Z-{COMPUTER}.repository"
#: A place of the thread's folder its computer's app set aside, by its name there: the folder's key, and when.
PLACE_NAME = "fcb59ee0818a927a.was-1791532800000"
#: What the newest of a thread's turn ends to record it recorded its computer keeping set aside of the thread's work.
SET_ASIDE_TOLD = {"copies": [REPOSITORY_NAME], "snapshots": ["a" * 40, "b" * 40], "gone": [COPY_NAME]}


def test_a_threads_row_says_what_its_computer_keeps_set_aside_of_its_work_as_its_newest_turn_end_to_record_it_did():
    given = replace(SHELL_FIXTURES["computer"][0], set_aside=SET_ASIDE_TOLD)
    assert derive_thread(given, now=NOW)["set_aside"] == {
        "kept": [{"kind": "repository", "at": "2026-10-10T10:15:01Z"}], "snapshots": 2,
        "gone": [{"kind": "copy", "at": "2026-10-10T10:15:00Z"}],
    }
    # It is a fact of its own: no event the row reads of its state says it, a command's end among them.
    command = event(31, "session.complete", reason="stop", answers=40)
    assert derive_thread(replace(given, events=(command,)), now=NOW)["set_aside"] == derive_thread(given, now=NOW)["set_aside"]
    assert "session.complete" not in LATEST_TYPES


def test_a_row_says_nothing_set_aside_where_its_turn_ends_recorded_none_or_none_of_it_is_a_name_or_a_time():
    assert all(derive_thread(given, now=NOW)["set_aside"] is None for given, _ in SHELL_FIXTURES.values())
    for told in (
        {"copies": [], "snapshots": [], "gone": []},
        {"copies": ["../../etc", f"00000001-20261010T101500Z-{COMPUTER}.history", 7], "snapshots": "all", "gone": None},
        # A name with more around it is none.
        {"copies": [f"../{COPY_NAME}", f"{COPY_NAME}.gone"], "snapshots": [], "gone": []},
        # Digits in a time's places that make no time.
        {"copies": [f"00000001-20261399T256199Z-{COMPUTER}.copy"], "snapshots": [], "gone": [f"00000002-20260230T101500Z-{COMPUTER}.copy"]},
        "everything",
    ):
        assert derive_thread(replace(SHELL_FIXTURES["computer"][0], set_aside=told), now=NOW)["set_aside"] is None, told
    assert set_aside_name(COPY_NAME) == {"kind": "copy", "at": "2026-10-10T10:15:00Z"}
    assert set_aside_name(f"00000001-20261010T246000Z-{COMPUTER}.copy") is None


def test_a_row_says_each_place_of_its_folder_its_computer_set_aside_among_what_it_keeps_by_when():
    given = replace(SHELL_FIXTURES["computer"][0], set_aside={"copies": [COPY_NAME], "snapshots": [], "gone": [], "places": [
        "0123456789abcdef.was-1791627301500", PLACE_NAME,
    ]})
    # Oldest first: a place by when it was set aside, to the second.
    assert derive_thread(given, now=NOW)["set_aside"] == {"kept": [
        {"kind": "place", "at": "2026-10-09T08:00:00Z"}, {"kind": "copy", "at": "2026-10-10T10:15:00Z"},
        {"kind": "place", "at": "2026-10-10T10:15:01Z"},
    ], "snapshots": 0, "gone": []}
    # A name that is no place's is left out, as is a place's among the copies; and with nothing left, nothing is said.
    for places in ([f"{PLACE_NAME}.json", f"../{PLACE_NAME}", "fcb59ee0818a927a.was-", f"fcb59ee0818a927a.was-{'9' * 16}", 7], "all"):
        told = {"copies": [PLACE_NAME], "snapshots": [], "gone": [], "places": places}
        assert derive_thread(replace(given, set_aside=told), now=NOW)["set_aside"] is None, places
