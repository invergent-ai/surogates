"""What a computer answers a thread's own kinds, taken as data: each answer built again from its own fields, or refused."""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from surogates.devices.binding import THREAD_ACTIONS, THREAD_KINDS
from surogates.devices.history import (
    ACTIONS,
    HISTORY_CODES,
    MAX_ASIDE,
    MAX_LISTED,
    MAX_LOOKED,
    MAX_PATH,
    MAX_WORDS,
    NO_COPY,
    REFUSALS,
    ComputerRefused,
    NotAnAnswer,
    Steps,
    ThreadCopy,
    answered,
    checked,
    code_of,
    refused,
)
from surogates.devices.operations import DeviceOperations, OperationConflict
from surogates.devices.workspace import RESULT_TRANSFERS, DeviceOperationError
from surogates.sandbox import history as sandbox_history
from surogates.sandbox import local_history
from surogates.sandbox.history import HistoryError

A, B, C = "a" * 40, "b" * 40, "c" * 40
THREAD = "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f"
ASIDE = f"00000003-20261010T101500Z-{THREAD}"
OWN = ".surogate-0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f.tmp"
VERSION = {"path": "Reports/Q3.docx", "before": A, "after": B}
REVISION = "2049:17:5:1700000000000000000:-1"
WHOLE_ASIDE = {"set_aside_folders": [f"{ASIDE}.copy", f"{ASIDE}.repository"], "set_aside_gone": [f"{ASIDE}.copy"]}

#: What an honest computer answers each action, in every shape it has: a check gives each back as it came.
HONEST: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("checkpoint", "take"): [{"hash": A}],
    ("checkpoint", "restore"): [{}],
    ("history", "open"): [
        {"copy": "made"},
        {"copy": "moved", "set_asides": [A, B]},
        {"copy": "kept", "set_asides": [A], **WHOLE_ASIDE},
        {"history": "off", "reason": "cap"},
        {"history": "off", "reason": "names", **WHOLE_ASIDE},
    ],
    ("history", "changed"): [{"paths": []}, {"paths": ["a.txt", "Reports/Q3.docx", "-rf", "a b/c\nd", "é/…", "$(id)"]}],
    ("history", "fetch"): [
        {"main": None, "landing": None, "hidden": False, "packs": 0, "missing": []},
        {"main": A, "landing": B, "hidden": True, "packs": 123_456, "missing": [C]},
    ],
    ("history", "pickup"): [
        {"main": None, "commit": None, "picked_up": [], "packs": 0},
        {"main": A, "commit": B, "picked_up": [VERSION, {"path": "new.md", "before": None, "after": C}], "packs": 7},
    ],
    ("history", "commit"): [
        {"commit": None, "base": A, "changes": [], "overlapped": [], "excluded": [], "repositories": [], "not_taken": []},
        {
            "commit": B, "base": A,
            # A file the thread deleted and you deleted too has no version on either side.
            "changes": [VERSION, {"path": "gone.md", "before": None, "after": None}, {"path": "old.md", "before": A, "after": None}],
            "overlapped": [
                {**VERSION, "reason": "changed", "by": {"kind": "you"}},
                {"path": "b.txt", "before": A, "after": None, "reason": "shape", "by": {"kind": "thread", "id": THREAD, "title": "Draft B"}},
                {"path": "c.txt", "before": None, "after": C, "reason": "with"},
                {"path": ".vscode/tasks.json", "before": None, "after": C, "reason": "protected"},
                {"path": "d.txt", "before": A, "after": B, "reason": "changed", "by": {"kind": "routine", "name": "Weekly digest"}},
            ],
            "excluded": ["node_modules/", ".env", "build/out.log"], "repositories": ["vendor/lib/"], "not_taken": ["notes.txt"],
        },
    ],
    ("history", "record"): [{"commit": A, "set_aside": None}, {"commit": A, "set_aside": B}],
    ("history", "keep"): [{"commit": A, "not_taken": []}, {"commit": A, "not_taken": ["notes.txt"]}],
    ("history", "forget"): [{"landing": None}, {"landing": A}],
    ("land", "recover"): [
        {"restored": [], "beside": [], "lost": [], "unread": []},
        {
            "restored": ["Report.docx"], "beside": [["Plan.docx", "Plan (kept by Surogate).docx"]],
            "lost": [["sub/a.txt", OWN]], "unread": [["saga:0f6d1c5e", 3, "a.txt"], ["hold:x", 0, None]],
        },
    ],
    ("land", "revisions"): [{"revisions": []}, {"revisions": [["a.txt", "absent"], ["b/c.txt", REVISION], ["d", "other"]]}],
    ("land", "apply"): [
        {**VERSION, "made": []},
        {"path": "Reports/2026/Q3/new.md", "before": None, "after": B, "made": ["Reports/2026/Q3", "Reports/2026"]},
        {"path": "old.md", "before": A, "after": None, "made": []},
    ],
    ("land", "unapply"): [{"path": "a.txt", "put_back": True}, {"path": "a.txt", "put_back": False}],
    ("land", "forget"): [{}],
}
EACH = [(kind, action, answer) for (kind, action), answers in HONEST.items() for answer in answers]
#: An answer to another action is no answer to one of these, whatever fields the two share.
EXACT = {("history", "forget"), ("land", "forget")}


def test_every_action_a_thread_asks_has_one_shape_of_answer_and_nothing_else_has_any():
    # The journal's list of what a thread asks, and this one's of what it takes for an answer, are one.
    assert ACTIONS == THREAD_ACTIONS and set(ACTIONS) == THREAD_KINDS
    assert {(kind, action) for kind, actions in ACTIONS.items() for action in actions} == set(HONEST)
    for kind, action in [("history", "prune"), ("history", "close"), ("history", "drop"), ("land", "open"), ("write", "open"), ("bind", "bind")]:
        with pytest.raises(NotAnAnswer):
            checked(kind, action, {"copy": "made", "commit": A, "hash": A})


@pytest.mark.parametrize(("kind", "action", "answer"), EACH)
def test_an_honest_answer_is_taken_as_it_came(kind, action, answer):
    assert checked(kind, action, answer, thread=THREAD) == answer


def noisy(value: Any) -> Any:
    """*value*, with fields no answer has beside its own, at every depth."""
    if isinstance(value, dict):
        more = {"session_id": "another", "folder": "/home/other", "device": {"id": 7}, "__proto__": {"x": 1}, "": None}
        return {**more, **{key: noisy(item) for key, item in value.items()}}
    if isinstance(value, list):
        return [noisy(item) for item in value]
    return value


@pytest.mark.parametrize(("kind", "action", "answer"), [each for each in EACH if each[:2] not in EXACT])
def test_an_answer_is_built_again_from_its_own_fields_and_nothing_else_of_it_is_kept(kind, action, answer):
    assert checked(kind, action, noisy(answer), thread=THREAD) == answer


@pytest.mark.parametrize(("kind", "action", "answer"), [
    # Not an answer at all.
    ("history", "open", None),
    ("history", "open", "made"),
    ("history", "open", ["made"]),
    ("history", "open", 7),
    # A word that is none of its answer's.
    ("history", "open", {"copy": "elsewhere"}),
    ("history", "open", {"copy": ["made"]}),
    ("history", "open", {"history": "off"}),
    ("history", "open", {"history": "off", "reason": "because"}),
    ("history", "open", {"history": "on", "reason": "cap"}),
    ("history", "open", {"copy": "elsewhere", "history": "off", "reason": "because"}),
    ("history", "commit", {**HONEST["history", "commit"][0], "overlapped": [{**VERSION, "reason": "because"}]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "overlapped": [{**VERSION, "reason": "changed", "by": {"kind": "admin"}}]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "overlapped": [{**VERSION, "reason": "changed", "by": "you"}]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "overlapped": [{**VERSION, "reason": "changed", "by": {"kind": "thread", "id": THREAD}}]}),
    # An id that is no commit's or blob's.
    ("checkpoint", "take", {"hash": "main"}),
    ("checkpoint", "take", {"hash": A.upper()}),
    ("checkpoint", "take", {"hash": A + "\n"}),
    ("checkpoint", "take", {"hash": A[:39]}),
    ("checkpoint", "take", {"hash": None}),
    ("history", "record", {"commit": "refs/heads/main; anything the computer likes", "set_aside": None}),
    ("history", "record", {"commit": A}),
    ("history", "pickup", {"main": "--upload-pack=/x", "commit": None, "picked_up": [], "packs": 0}),
    ("history", "pickup", {"main": A, "commit": None, "picked_up": [{"path": "a", "before": "HEAD", "after": None}], "packs": 0}),
    ("history", "commit", {**HONEST["history", "commit"][0], "base": None}),
    ("history", "fetch", {**HONEST["history", "fetch"][0], "missing": [A, "HEAD"]}),
    ("history", "open", {"copy": "kept", "set_asides": [A, 7]}),
    # A version that is not said is not "none".
    ("history", "pickup", {"main": A, "commit": None, "picked_up": [{"path": "a", "after": None}], "packs": 0}),
    ("history", "commit", {**HONEST["history", "commit"][0], "changes": [{"path": "a", "before": A}]}),
    ("land", "apply", {"path": "a.txt", "after": B, "made": []}),
    ("history", "fetch", {"landing": None, "hidden": False, "packs": 0, "missing": []}),
    # A path that is no file's in the folder.
    ("history", "changed", {"paths": ["../../etc/passwd"]}),
    ("history", "changed", {"paths": ["/etc/passwd"]}),
    ("history", "changed", {"paths": ["a/../b"]}),
    ("history", "changed", {"paths": ["a\0b"]}),
    ("history", "changed", {"paths": ["a//b"]}),
    ("history", "changed", {"paths": ["a/"]}),
    ("history", "changed", {"paths": ["./a"]}),
    ("history", "changed", {"paths": [""]}),
    ("history", "changed", {"paths": ["half a character \ud83d"]}),
    ("history", "changed", {"paths": ["x" * (MAX_PATH + 1)]}),
    ("history", "changed", {"paths": "Report.docx"}),
    ("history", "changed", {"paths": [["Report.docx"]]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "changes": [{"path": "..", "before": A, "after": B}]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "excluded": ["node_modules/", 7]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "excluded": ["/etc/"]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "repositories": ["../vendor/"]}),
    ("history", "keep", {"commit": A, "not_taken": ["/root/.ssh/id_rsa"]}),
    ("land", "revisions", {"revisions": [["../a.txt", "absent"]]}),
    ("land", "apply", {**VERSION, "made": ["/home/u/Reports/new"]}),
    ("land", "apply", {**VERSION, "made": ["Reports/.."]}),
    ("land", "unapply", {"path": "/etc/passwd", "put_back": True}),
    ("land", "recover", {**HONEST["land", "recover"][0], "restored": ["/etc/passwd"]}),
    ("land", "recover", {**HONEST["land", "recover"][0], "beside": [["a.txt", "../a (kept by Surogate).txt"]]}),
    ("land", "recover", {**HONEST["land", "recover"][0], "lost": [["a.txt", "../../x"]]}),
    ("land", "recover", {**HONEST["land", "recover"][0], "unread": [["saga; rm -rf /", 0, None]]}),
    ("land", "recover", {**HONEST["land", "recover"][0], "unread": [["saga:1", -1, None]]}),
    ("land", "recover", {**HONEST["land", "recover"][0], "unread": [["saga:1", 0, "/etc/passwd"]]}),
    # A number, or a yes, that is none.
    ("history", "pickup", {"main": A, "commit": None, "picked_up": [], "packs": -1}),
    ("history", "pickup", {"main": A, "commit": None, "picked_up": [], "packs": True}),
    ("history", "pickup", {"main": A, "commit": None, "picked_up": [], "packs": 1.5}),
    ("history", "pickup", {"main": A, "commit": None, "picked_up": [], "packs": 2**80}),
    ("history", "fetch", {**HONEST["history", "fetch"][0], "hidden": "no"}),
    ("history", "fetch", {**HONEST["history", "fetch"][0], "hidden": 0}),
    ("land", "unapply", {"path": "a.txt", "put_back": "yes"}),
    ("land", "unapply", {"path": "a.txt", "put_back": 1}),
    # A look that is none.
    ("land", "revisions", {"revisions": [["a.txt", "1:2:3"]]}),
    ("land", "revisions", {"revisions": [["a.txt"]]}),
    ("land", "revisions", {"revisions": [["a.txt", "absent", "other"]]}),
    ("land", "revisions", {"revisions": [["a.txt", "1" * 200 + ":1:1:1:1"]]}),
    ("land", "revisions", {"revisions": {"a.txt": "absent"}}),
    ("land", "apply", {"path": "a.txt", "before": A, "after": "x", "made": []}),
    ("land", "apply", {"path": "a.txt", "before": A, "after": B}),
    # More than an answer holds.
    ("history", "changed", {"paths": ["a"] * (MAX_LISTED + 1)}),
    ("history", "changed", {"paths": ["a"] * 1_000_000}),
    ("history", "commit", {**HONEST["history", "commit"][0], "changes": [VERSION] * (MAX_LISTED + 1)}),
    ("land", "revisions", {"revisions": [["a.txt", "absent"]] * (MAX_LOOKED + 1)}),
    ("history", "open", {"copy": "kept", "set_aside_folders": [f"{ASIDE}.copy"] * (MAX_ASIDE + 1)}),
    ("history", "commit", {**HONEST["history", "commit"][0], "overlapped": [
        {**VERSION, "reason": "changed", "by": {"kind": "thread", "id": THREAD, "title": "x" * (MAX_PATH + 1)}},
    ]}),
    ("history", "commit", {**HONEST["history", "commit"][0], "overlapped": [
        {**VERSION, "reason": "changed", "by": {"kind": "routine", "name": "megabytes " * 500_000}},
    ]}),
    # What was set aside of another thread's, or told with nothing to say whose it is.
    ("history", "open", {"copy": "kept", "set_aside_folders": [f"00000003-20261010T101500Z-{uuid4()}.copy"]}),
    ("history", "open", {"copy": "kept", "set_aside_gone": [f"../{ASIDE}.copy"]}),
    ("history", "open", {"copy": "kept", "set_aside_folders": [f"{ASIDE}.history"]}),
    ("history", "open", {"history": "off", "reason": "cap", "set_aside_gone": ["everything"]}),
    # Another action's answer.
    ("history", "forget", HONEST["history", "fetch"][1]),
    ("history", "forget", {"landing": A, "main": A}),
    ("history", "forget", {}),
    ("land", "forget", HONEST["history", "fetch"][1]),
    ("land", "forget", {"landing": None}),
    ("history", "record", HONEST["history", "keep"][0]),
    ("history", "open", HONEST["history", "record"][0]),
    ("history", "fetch", {"main": A, "has_saga": True, "packs": 0}),
    ("history", "keep", {"commit": A, "files": [VERSION]}),
])
def test_what_is_no_answer_is_refused(kind, action, answer):
    with pytest.raises(NotAnAnswer) as refusal:
        checked(kind, action, answer, thread=THREAD)
    # Its own words, and none of the computer's.
    assert str(refusal.value) == f"This computer's answer to '{action}' was not one"


def test_what_was_set_aside_is_named_only_to_a_check_that_knows_whose_copy_was_asked_about():
    told = {"copy": "kept", **WHOLE_ASIDE}
    assert checked("history", "open", told, thread=THREAD) == told
    for unknown in (None, "another", str(uuid4()), THREAD.upper(), f"{THREAD}|.*"):
        with pytest.raises(NotAnAnswer):
            checked("history", "open", told, thread=unknown)
    # A thread is a session's id: a name is none of what is no thread's, though it ends as that is spelt.
    for no_thread in ("", "../x", "x"):
        with pytest.raises(NotAnAnswer):
            checked("history", "open", {"copy": "kept", "set_aside_gone": [f"00000003-20261010T101500Z-{no_thread}.copy"]}, thread=no_thread)
    # Nothing set aside, nothing to tell whose.
    assert checked("history", "open", {"copy": "kept", "set_asides": [A]}) == {"copy": "kept", "set_asides": [A]}


def test_an_open_is_taken_in_the_first_of_its_two_forms_that_holds_as_the_app_takes_it():
    assert checked("history", "open", {"copy": "made", "history": "off", "reason": "cap"}) == {"copy": "made"}
    assert checked("history", "open", {"copy": "elsewhere", "history": "off", "reason": "cap"}) == {"history": "off", "reason": "cap"}
    assert checked("history", "open", {"copy": "made", "set_asides": ["main"], "history": "off", "reason": "names"}) == {
        "history": "off", "reason": "names",
    }


#: What a computer can put where an answer's field should be.
GARBAGE: list[Any] = [
    None, True, False, 0, -1, 1.5, 2**63, float("inf"), "", "x", "/etc/passwd", "../x", "a\0b", "\ud800", "x" * (MAX_PATH + 1),
    A.upper(), "HEAD", "--upload-pack=/x", [], [[]], [None], ["x", "y"], [["x", "y", "z"]], {}, {"a": 1}, {"kind": "you"},
]


def mutations(value: Any):
    """*value* with one of its parts another thing, for each part and each thing; and with each field left out."""
    for other in GARBAGE:
        yield other
    if isinstance(value, dict):
        for key in value:
            yield {name: item for name, item in value.items() if name != key}
            for changed in mutations(value[key]):
                yield {**value, key: changed}
    elif isinstance(value, list):
        for index in range(len(value)):
            for changed in mutations(value[index]):
                yield [*value[:index], changed, *value[index + 1:]]


def data(value: Any) -> None:
    """*value* is what a row, a report or a path may hold: bounded words and lists, of text that is text."""
    if isinstance(value, str):
        assert len(value) <= MAX_PATH and "\0" not in value
        value.encode()
    elif isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            data(item)
    elif isinstance(value, list):
        assert len(value) <= MAX_LISTED
        for item in value:
            data(item)
    else:
        assert value is None or type(value) in (bool, int)


@pytest.mark.parametrize(("kind", "action", "answer"), EACH)
def test_whatever_a_computer_puts_in_an_answers_place_is_refused_or_taken_as_data_and_nothing_else_happens(kind, action, answer):
    for mutant in mutations(answer):
        try:
            taken = checked(kind, action, mutant, thread=THREAD)
        except NotAnAnswer:
            continue
        # What is taken is an answer still: checked again it is itself, and it holds nothing but data.
        assert checked(kind, action, copy.deepcopy(taken), thread=THREAD) == taken, mutant
        assert set(taken) <= {key for honest in HONEST[kind, action] for key in honest}, mutant
        data(taken)


def test_an_answer_read_from_the_journal_is_checked_as_one_given_now():
    # The journal holds what a computer answered, as it answered: a recorded answer is its word still.
    assert answered("history", "record", {"ok": {"commit": A, "set_aside": None, "session": "another"}}) == {"commit": A, "set_aside": None}
    assert answered("history", "record", {"ok": {"commit": "refs/heads/main; anything the computer likes", "set_aside": None}}) is None
    assert answered("history", "record", {"error": {"type": "history", "code": "failed", "message": "no"}}) is None
    assert answered("history", "open", {"ok": {"history": "off", "reason": "because"}}) is None
    assert answered("history", "open", {"ok": {"copy": "kept", **WHOLE_ASIDE}}, thread=THREAD) == {"copy": "kept", **WHOLE_ASIDE}
    assert answered("history", "open", {"ok": {"copy": "kept", **WHOLE_ASIDE}}) is None
    for none in (None, "ok", [], {}, {"ok": None}, {"okay": {"commit": A}}, {"ok": {"commit": A, "set_aside": None}, "error": {}}):
        assert answered("history", "record", none) is None, none
    assert answered("history", "prune", {"ok": {"pruned": True}}) is None


class Runner:
    """A journal's runner that answers every step *outcome*, and keeps what it was asked."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.asked: list[tuple[str, dict]] = []

    async def run(self, kind: str, args: dict[str, Any]) -> Any:
        self.asked.append((kind, args))
        return self.outcome


def test_a_step_asks_its_action_with_its_arguments_and_answers_what_was_checked():
    runner = Runner({"ok": {"commit": A, "set_aside": None, "folder": "/home/other"}})
    steps = Steps(runner)
    assert asyncio.run(steps.history("record", turn=B, applied=[VERSION])) == {"commit": A, "set_aside": None}
    assert runner.asked == [("history", {"action": "record", "turn": B, "applied": [VERSION]})]
    looked = Runner({"ok": HONEST["land", "revisions"][1]})
    assert asyncio.run(Steps(looked).land("revisions", paths=["a.txt"])) == HONEST["land", "revisions"][1]
    assert looked.asked == [("land", {"action": "revisions", "paths": ["a.txt"]})]
    taken = Runner({"ok": {"hash": A}})
    assert asyncio.run(Steps(taken).ask("checkpoint", "take", reason="before write_file")) == {"hash": A}
    for none in ({"ok": {"commit": "main", "set_aside": None}}, {"ok": None}, {}, None, "ok", {"okay": 1}):
        with pytest.raises(NotAnAnswer):
            asyncio.run(Steps(Runner(none)).history("record"))


def refusal(error: Any, kind: str = "history", action: str = "open") -> tuple[str, str | None, str]:
    with pytest.raises(ComputerRefused) as caught:
        asyncio.run(Steps(Runner({"error": error})).ask(kind, action))
    return caught.value.kind, caught.value.code, str(caught.value)


def test_a_refusal_is_read_by_its_type_and_its_code_and_never_by_its_words():
    assert refusal({"type": "history", "code": "no_whole_copy", "message": "no"}) == ("history", "no_whole_copy", "no")
    assert refusal({"type": "busy", "message": "Another chat is working here"}) == ("busy", None, "Another chat is working here")
    assert refusal({"type": "unsupported", "message": "This chat works in its folder itself"})[:2] == (NO_COPY, None)
    assert refusal({"type": "os", "code": "EFBIG", "message": "File too large to land"})[:2] == ("os", "EFBIG")
    # The server's own, for an operation it closed itself.
    assert refusal({"type": "cancelled", "message": "Stopped before the computer reported a result."})[:2] == ("cancelled", None)
    assert refusal({"type": "revoked", "message": "This computer's access was revoked"})[:2] == ("revoked", None)
    # A code is taken only where its type has codes, and only from that type's own.
    assert refusal({"type": "busy", "code": "no_whole_copy", "message": "no"})[:2] == ("busy", None)
    assert refusal({"type": "os", "code": "no_whole_copy", "message": "no"})[:2] == ("os", None)
    assert refusal({"type": "os", "code": "ERR_FS_EISDIR", "message": "no"})[:2] == ("os", None)
    # A refusal of the history's always has one of its codes: with any other it is no refusal of a history's.
    assert refusal({"type": "history", "code": "landed", "message": "this landing is recorded"})[:2] == ("other", None)
    assert refusal({"type": "history", "code": "EFBIG", "message": "no"})[:2] == ("other", None)
    assert refusal({"type": "history", "message": "no"})[:2] == ("other", None)
    # What is no type of a refusal's is not carried into a report, a row or a decision: it stands as one of no known kind.
    said = "This computer refused it"
    assert refusal({"type": "busy; DROP TABLE sessions", "code": "<b>x</b>", "message": 7}) == ("other", None, said)
    assert refusal({"type": "completed", "message": "no"})[:2] == ("other", None)
    assert refusal({"type": "x" * 65, "code": ["a"]}) == ("other", None, said)
    assert refusal({"type": ["busy"], "code": {"history": 1}, "message": ["megabytes"] * 100_000}) == ("other", None, said)
    for error in ("no", None, 7, [], {}):
        assert refusal(error) == ("other", None, said)


def test_a_refusals_words_are_cut_to_what_a_person_reads_and_are_text():
    kind, _, words = refusal({"type": "busy", "message": "megabytes " * 500_000})
    assert (kind, len(words)) == ("busy", MAX_WORDS)
    assert refusal({"type": "busy", "message": "a\0b"})[2] == "ab"
    assert refusal({"type": "busy", "message": "half a character \ud83d"})[2] == "This computer refused it"
    assert refusal({"type": "busy", "message": ""})[2] == "This computer refused it"


def test_every_type_and_code_a_refusal_may_have_is_a_word_of_the_servers_own():
    # Read by `in`, so no computer's word reaches a report: each is spelt here, as the app and its history spell theirs.
    assert REFUSALS == {
        "history", "busy", "history_off", "unsupported", "refused", "value", "conflict", "stale", "sandbox", "os", "too_large",
        "cancelled", "interrupted", "unavailable", "folder_unavailable", "revoked", "other",
    }
    assert HISTORY_CODES == {
        "failed", "history_refused", "conflict", "no_whole_copy", "name_not_utf8", "not_a_request", "record_unfinished",
        "move_unfinished", "landing_unsettled", "not_on_base", "no_answer", "not_an_answer",
    }
    for kind in sorted(REFUSALS - {"history"}):
        assert refusal({"type": kind, "message": "no"})[:2] == (kind, None)
    for code in sorted(HISTORY_CODES):
        assert refusal({"type": "history", "code": code, "message": "no"})[:2] == ("history", code)


def test_a_refusal_read_from_the_journal_is_read_as_one_given_now():
    assert refused({"ok": {"copy": "made"}}) is None
    assert refused(None) is None and refused("error") is None and refused({}) is None
    read = refused({"error": {"type": "unsupported", "message": "This chat works in its folder itself"}})
    assert (read.kind, read.code) == (NO_COPY, None)
    read = refused({"error": {"type": "history", "code": "<b>", "message": "x" * 5_000}})
    assert (read.kind, read.code, len(str(read))) == ("other", None, MAX_WORDS)


def test_a_report_names_a_refusal_by_a_word_of_the_servers_own():
    assert code_of(ComputerRefused("history", "no", "no_answer")) == "no_answer"
    assert code_of(ComputerRefused("os", "File too large", "EDQUOT")) == "EDQUOT"
    assert code_of(ComputerRefused("busy", "Another chat is working here")) == "busy"
    assert code_of(NotAnAnswer("This computer's answer to 'open' was not one")) == "not_an_answer"
    # The journal's own refusal: its computer was asked nothing.
    assert code_of(DeviceOperationError("This session was stopped")) == "not_asked"


def test_none_of_a_threads_kinds_is_answered_with_a_transfer():
    # The link refuses a result that names a transfer for any kind but these, so the journal's runner never reads
    # one for a thread's kind: a thread's answers are whole, in one frame.
    assert THREAD_KINDS.isdisjoint(RESULT_TRANSFERS)


class Journal(DeviceOperations):
    """A journal that records nothing: each request it is asked to run, answered *outcome*."""

    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome
        self.requests: list = []

    async def run(self, request, *, keep_open: bool = False) -> dict[str, Any]:
        self.requests.append(request)
        return self.outcome


def a_session(*, under: UUID | None = None, **execution: Any) -> SimpleNamespace:
    """A session on a computer; *under* another, it works in that one's sandbox."""
    session_id = uuid4()
    return SimpleNamespace(id=session_id, parent_id=under, config={
        "execution": {"kind": "device", "device_id": str(uuid4()), **execution},
        "workspace_path": "/home/u/Reports", "sandbox_root_session_id": str(under or session_id),
    })


def a_thread() -> SimpleNamespace:
    thread = a_session()
    thread.config["execution"]["history"] = {"thread": str(thread.id)}
    return thread


def test_a_threads_copy_is_asked_about_under_its_turns_own_invocations_as_the_session_that_asks():
    thread = a_thread()
    journal = Journal({"ok": {"copy": "moved"}})
    its = ThreadCopy(journal, thread, lease_token="lease-1")
    assert (its.thread, its.device_id, its.folder) == (thread.id, UUID(thread.config["execution"]["device_id"]), "/home/u/Reports")
    assert asyncio.run(its.open(7)) == {"copy": "moved"}
    # Asked again after a refusal, it is another asking, under the turn's next name.
    assert asyncio.run(its.open(7, again=2)) == {"copy": "moved"}
    journal.outcome = {"ok": {"hash": A}}
    assert asyncio.run(its.take(7, 3, "call_1", "before write_file " + "x" * 500)) == A
    journal.outcome = {"ok": {}}
    assert asyncio.run(its.restore(7, A)) is None
    asked = [(r.invocation_id, r.ordinal, r.kind, r.args) for r in journal.requests]
    assert asked == [
        ("open:7", 1, "history", {"action": "open"}),
        ("open:7:2", 1, "history", {"action": "open"}),
        ("checkpoint:7:3:call_1", 1, "checkpoint", {"action": "take", "reason": ("before write_file " + "x" * 500)[:200]}),
        (f"checkpoint:7:restore:{A}", 1, "checkpoint", {"action": "restore", "hash": A}),
    ]
    # Each from the session the server stamped: its computer, its root, itself, and the lease its worker holds.
    for request in journal.requests:
        assert (request.device_id, request.root_session_id, request.calling_session_id, request.lease_token) == (
            its.device_id, thread.id, thread.id, "lease-1",
        )
    # The steps of one invocation are numbered in the order asked, so a turn resumed asks each again by its number.
    landing = its.steps("land:7")
    journal.outcome = {"ok": {"paths": []}}
    asyncio.run(landing.history("changed"))
    journal.outcome = {"ok": {"revisions": []}}
    asyncio.run(landing.land("revisions", paths=[]))
    assert [(r.invocation_id, r.ordinal, r.kind) for r in journal.requests[-2:]] == [("land:7", 1, "history"), ("land:7", 2, "land")]
    with pytest.raises(ValueError, match="A snapshot is named by its commit"):
        asyncio.run(its.restore(7, "main"))


def test_what_was_set_aside_of_a_threads_copy_is_told_only_as_that_threads_own():
    thread = a_thread()
    aside = f"00000003-20261010T101500Z-{thread.id}.copy"
    its = ThreadCopy(Journal({"ok": {"copy": "kept", "set_aside_folders": [aside]}}), thread, lease_token=None)
    assert asyncio.run(its.open(0)) == {"copy": "kept", "set_aside_folders": [aside]}
    other = ThreadCopy(Journal({"ok": {"copy": "kept", "set_aside_folders": [f"{ASIDE}.copy"]}}), thread, lease_token=None)
    with pytest.raises(NotAnAnswer):
        asyncio.run(other.open(0))


def test_a_session_under_a_thread_asks_as_itself_about_the_threads_copy():
    thread = a_thread()
    helper = a_session(under=thread.id, history={"thread": str(thread.id)})
    journal = Journal({"ok": {"hash": A}})
    asyncio.run(ThreadCopy(journal, helper, lease_token=None).take(7, 0, "call_1", "before a step"))
    [request] = journal.requests
    assert (request.root_session_id, request.calling_session_id) == (thread.id, helper.id)


def test_only_a_session_the_server_marked_as_working_in_a_copy_has_one_to_ask_about():
    cloud = SimpleNamespace(id=uuid4(), parent_id=None, config={"workstream_role": "thread"})
    chat = a_session()
    # A session whose mark names a copy that is not its root's: no answer of that computer is its own.
    stray = a_session(history={"thread": str(uuid4())})
    for session in (cloud, chat, stray):
        with pytest.raises(ValueError, match="Only a session of a thread that works in a copy of its own has one to ask about"):
            ThreadCopy(Journal({"ok": {"copy": "made"}}), session, lease_token=None)


def test_a_step_asked_on_another_path_than_its_first_run_is_the_journals_refusal_and_is_not_answered():
    class Conflicted(Journal):
        async def run(self, request, *, keep_open: bool = False):
            raise OperationConflict("Operation 1 of land:7 was recorded with a different request")

    with pytest.raises(OperationConflict):
        asyncio.run(ThreadCopy(Conflicted({}), a_thread(), lease_token=None).steps("land:7").history("changed"))


def test_the_bounds_of_an_answer_are_the_ones_said():
    assert (MAX_LISTED, MAX_LOOKED, MAX_ASIDE, MAX_PATH, MAX_WORDS) == (50_000, 2_000, 64, 4_096, 2_000)
    assert len(checked("history", "changed", {"paths": ["a"] * MAX_LISTED})["paths"]) == MAX_LISTED
    assert checked("history", "changed", {"paths": ["x" * MAX_PATH]}) == {"paths": ["x" * MAX_PATH]}
    assert len(checked("land", "revisions", {"revisions": [["a", "absent"]] * MAX_LOOKED})["revisions"]) == MAX_LOOKED
    whole = {"copy": "kept", "set_aside_gone": [f"{ASIDE}.copy"] * MAX_ASIDE}
    assert checked("history", "open", whole, thread=THREAD) == whole
    by = {"kind": "thread", "id": THREAD, "title": "x" * MAX_PATH}
    held = {**HONEST["history", "commit"][0], "overlapped": [{**VERSION, "reason": "changed", "by": by}]}
    assert checked("history", "commit", held) == held


def history_codes() -> set[str]:
    """Every code the folder's history refuses with, as its two modules spell them."""
    return {
        value for module in (sandbox_history, local_history) for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str) and value == name.lower()
    }


def test_every_code_the_folders_history_has_and_every_action_it_takes_is_one_this_module_knows():
    # Its ten, and none it says to a person alone: the guest's and the app's own are beside them.
    assert history_codes() == HISTORY_CODES - {"no_answer", "not_an_answer"}
    # A thread's history steps and its snapshots, as the app passes them on: none is asked that the history
    # does not take, and the history takes none that is not asked and checked here.
    assert set(local_history._ACTIONS) == ACTIONS["history"] | {"snapshot", "restore"}


class Folder:
    """A folder of the user's and its place, the history in it run as the guest runs it, each answer taken as the server takes it."""

    def __init__(self, base: Path, *files: tuple[str, bytes]) -> None:
        self.real = base / "Documents"
        self.real.mkdir(parents=True)
        for name, data in files:
            (self.real / name).parent.mkdir(parents=True, exist_ok=True)
            (self.real / name).write_bytes(data)
        self.store = base / "place"

    def copy(self, thread: str) -> Path:
        return self.store / "threads" / thread

    def run(self, thread: str, action: str, **args: Any) -> dict:
        """The history's answer, or its refusal as the app passes one on, by its code."""
        try:
            return {"ok": local_history.run({
                "store": str(self.store), "folder": str(self.real), "thread": thread, "user": "u1", "action": action, "args": args,
            })}
        except HistoryError as refused:
            return {"error": {"type": "history", "code": refused.code, "message": str(refused)}}

    def taken(self, thread: str, kind: str, action: str, **args: Any) -> dict:
        """*action*, asked of the history, its answer taken as the server takes it: whole, and nothing of it dropped."""
        asked = {("checkpoint", "take"): "snapshot", ("checkpoint", "restore"): "restore"}.get((kind, action), action)
        outcome = self.run(thread, asked, **({"commit": args.pop("hash")} if asked == "restore" else args))
        assert "ok" in outcome, outcome
        assert checked(kind, action, outcome["ok"], thread=thread) == outcome["ok"]
        return outcome["ok"]

    def refused(self, thread: str, action: str, **args: Any) -> str:
        outcome = self.run(thread, action, **args)
        refusal = refused(outcome)
        assert refusal is not None and (refusal.kind, refusal.code) == ("history", outcome["error"]["code"]), outcome
        return refusal.code


def landed(folder: Folder, thread: str, turn: dict) -> None:
    """The applies of *turn*, as a landing's helper makes them: each file of the copy's, or none."""
    for change in turn["changes"]:
        target = folder.real / change["path"]
        if change["after"] is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder.copy(thread) / change["path"], target)


def test_what_the_folders_own_history_answers_a_thread_is_taken_whole_and_each_refusal_by_its_code(tmp_path):
    folder = Folder(tmp_path, ("Report.docx", b"PK report v1"), ("notes.txt", b"v1 notes\n"))
    one, two = str(uuid4()), str(uuid4())
    a = {"name": "Draft A", "email": f"thread:{one}@surogate"}
    b = {"name": "Draft B", "email": f"thread:{two}@surogate"}
    yours = {"name": "u1", "email": "user:u1@surogate"}
    assert folder.taken(one, "history", "open") == {"copy": "made"}
    assert folder.taken(two, "history", "open") == {"copy": "made"}
    # A step of thread one's, its snapshot first.
    first = folder.taken(one, "checkpoint", "take", reason="before write_file")["hash"]
    (folder.copy(one) / "Report.docx").write_bytes(b"PK report v2, by A")
    (folder.copy(one) / "Reports").mkdir()
    (folder.copy(one) / "Reports" / "Q3.md").write_text("Q3\n")
    (folder.copy(one) / "node_modules" / "x").mkdir(parents=True)
    (folder.copy(one) / "node_modules" / "x" / "index.js").write_text("x\n")
    (folder.copy(one) / "notes.txt").unlink()
    assert folder.taken(one, "history", "changed") == {"paths": ["Report.docx", "Reports/Q3.md", "notes.txt"]}
    # Its landing, step by step, as the worker will ask it.
    saga = f"saga:{uuid4()}"
    trailers = [["Surogate-Saga", saga], ["Surogate-Thread", one]]
    picked = folder.taken(one, "history", "pickup", author=yours, trailers=[*trailers, ["Surogate-Kind", "pickup"]])
    turn = folder.taken(one, "history", "commit", author=a, trailers=[*trailers, ["Surogate-Kind", "turn"]], pickup=picked["commit"])
    # Each file the turn changed lands, or is left out with why.
    assert sorted(change["path"] for change in turn["changes"] + turn["overlapped"]) == ["Report.docx", "Reports/Q3.md", "notes.txt"]
    assert turn["excluded"] == ["node_modules/"]
    assert folder.taken(one, "history", "fetch", saga=saga, since=picked["main"])["landing"] is None
    # Not recorded, and not put back: what it kept is not to be forgotten.
    landed(folder, one, turn)
    assert folder.refused(one, "forget", saga=saga, applied=turn["changes"]) == "landing_unsettled"
    recorded = folder.taken(
        one, "history", "record", turn=turn["commit"], applied=turn["changes"], author=a,
        trailers=[*trailers, ["Surogate-Kind", "landing"]], main=picked["main"], pickup=picked["commit"],
    )
    assert folder.taken(one, "history", "fetch", saga=saga, since=picked["main"])["landing"] == recorded["commit"]
    assert folder.taken(one, "history", "forget", saga=saga, applied=turn["changes"]) == {"landing": recorded["commit"]}
    # The folder held for a landing, and let go with nothing kept.
    assert folder.taken(one, "history", "forget", saga=f"hold:{uuid4()}", applied=[]) == {"landing": None}
    # A snapshot from before the landing is not one the copy is put back to.
    assert folder.refused(one, "restore", commit=first) == "not_on_base"
    # Thread two changed the file one landed: its commit says who changed it since.
    (folder.copy(two) / "Report.docx").write_bytes(b"PK report v2, by B")
    saga = f"saga:{uuid4()}"
    trailers = [["Surogate-Saga", saga], ["Surogate-Thread", two]]
    picked = folder.taken(two, "history", "pickup", author=yours, trailers=[*trailers, ["Surogate-Kind", "pickup"]])
    turn = folder.taken(two, "history", "commit", author=b, trailers=[*trailers, ["Surogate-Kind", "turn"]], pickup=picked["commit"])
    assert [(o["path"], o["reason"], o["by"]) for o in turn["overlapped"]] == [
        ("Report.docx", "changed", {"kind": "thread", "id": one, "title": "Draft A"}),
    ]
    # Its turn kept, as a failed turn's is; its copy then keeps its work at its next open.
    kept = folder.taken(two, "history", "keep", author=b, trailers=[*trailers, ["Surogate-Kind", "kept"]], base=False)
    assert kept["not_taken"] == []
    assert folder.taken(two, "history", "open") == {"copy": "kept"}
    taken = folder.taken(two, "checkpoint", "take", reason="before patch")["hash"]
    (folder.copy(two) / "Report.docx").write_bytes(b"PK a step's change")
    assert folder.taken(two, "checkpoint", "restore", hash=taken) == {}
    assert (folder.copy(two) / "Report.docx").read_bytes() == b"PK report v2, by B"
    # What is no request, and a copy that is not whole.
    assert folder.refused(two, "prune") == "not_a_request"
    assert folder.refused(two, "restore", commit="f" * 40) == "failed"
    shutil.rmtree(folder.copy(two))
    assert folder.refused(two, "changed") == "no_whole_copy"


def test_a_folder_the_history_cannot_record_is_said_so_in_an_answer_this_module_takes(tmp_path):
    folder = Folder(tmp_path, ("Report.docx", b"PK report v1"))
    os.close(os.open(os.fsencode(folder.real) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY, 0o644))
    thread = str(uuid4())
    assert folder.taken(thread, "history", "open") == {"history": "off", "reason": "names"}
