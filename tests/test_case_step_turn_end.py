"""A case step's turn that ends without a report, and one that fails.

A step ends only with ``complete_step`` or ``raise_hand``.  A turn that ends
in words is asked once to make the report, and goes on.  One that still
ends without it, or fails, is told to Keel through its door: ``no-outcome``
with the turn end's code and the step's usage, or ``failed`` with the
failure's own code, never its text.  A failed turn takes the step's inputs
out of the session.  A turn whose report ended the step tells Keel nothing
more, and Keel being away never keeps a turn from ending.  A session the
dispatcher fails, its wakes crashing or never recovering, is told to Keel as
a failed turn is.

A report that ended the step for this round marks the session's row until
Keel writes to the step again: a marked step is never nudged, and a crash
or a failure never reports it or takes its inputs, so a draft under review
or a raised hand can still take the person's word.  Keel's word written as
the reporting turn ends lies past that turn's cursor: the wake it queued
runs, and the model reads it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

import surogates.keel.client as keel_client_module
from surogates.keel.client import KeelClient, keel_client, use_keel_client
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.events import EventType
from surogates.storage.backend import LocalBackend
from tests.test_case_step_isolation import CONTRACT
from tests.test_case_step_tools import (
    ACCEPTED,
    COMPLETION,
    DRAFTED,
    GIVEN_BACK,
    HAND_RAISED,
    USAGE,
    _batch,
    _calls,
    _ended_with,
    _FakeKeel,
    _queued_wake,
    _QueuedStore,
    _started,
    _step,
    _StepStore,
    _wake,
)
from tests.test_orchestrator_crash_loop import FakeRedis

NUDGE = (
    "This session works one step of a case. Finish it now: call complete_step "
    "with one of its outcomes, or raise_hand if you cannot."
)
# What the model said before its provider cut the answer off: the case's own data.
CUT_OFF = "Ioana's declared income of 182000.00 does not match the"


@pytest.fixture
def fake_keel():
    keel = _FakeKeel()
    previous = keel_client()
    use_keel_client(KeelClient("http://keel-door.test", "door-key", transport=httpx.MockTransport(keel.handle)))
    yield keel
    use_keel_client(previous)


@pytest.fixture
def store(tmp_path) -> _StepStore:
    return _StepStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))


def _says(text: str):
    """The model's answer ending its turn in words."""
    return (
        {"role": "assistant", "content": text},
        {"model": "test-model", "finish_reason": "stop", "input_tokens": 1, "output_tokens": 1},
    )


def _cut_off():
    """The provider failing part-way through the model's answer."""
    return (
        {"role": "assistant", "content": CUT_OFF},
        {"model": "test-model", "finish_reason": "error", "input_tokens": 1, "output_tokens": 1},
    )


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content or ""


def _last_user_message(request: dict) -> str:
    return _text([m for m in request["messages"] if m["role"] == "user"][-1])


def _failed_with(store: _StepStore) -> str:
    kind, data = store.events[-1][1:]
    assert kind is EventType.SESSION_FAIL
    return data["reason"]


async def test_a_turn_without_an_outcome_is_nudged_once_then_reported(fake_keel, monkeypatch, store):
    sent = await _wake(monkeypatch, store, [
        _says("I read the checklist and the payslips."),
        _says("The documents look complete to me."),
    ])

    assert len(sent) == 2
    assert _last_user_message(sent[1]) == NUDGE
    assert json.dumps(sent[1]["messages"]).count("Finish it now") == 1
    # Kept in the step's log for its later wakes to read, as the harness's own: it never steers or revives.
    [nudge] = [e.data for e in store.log if e.type == EventType.USER_MESSAGE.value and e.data["content"] == NUDGE]
    assert nudge["synthetic"]
    assert _ended_with(store) == "completed"
    assert store.row.status == "completed"
    assert fake_keel.bodies("/ended") == [
        {"reason": "no-outcome", "detail": "completed", "usage": USAGE, "bundle_version": None},
    ]
    assert len(fake_keel.requests) == 1
    # Keel decides what comes of the step: it is still the session's to take up.
    assert not store.redacted()


async def test_a_failed_turn_is_reported_as_failed(fake_keel, monkeypatch, store):
    sent = await _wake(monkeypatch, store, [_cut_off(), _cut_off(), _cut_off()])

    assert len(sent) == 3
    assert _failed_with(store) == "provider_error"
    assert store.row.status == "failed"
    assert fake_keel.bodies("/ended") == [
        {"reason": "failed", "detail": "provider_error", "usage": USAGE, "bundle_version": None},
    ]
    # Nothing of the error, nor of the case, reaches Keel.
    for request in fake_keel.requests:
        body = request.content.decode()
        for fragment in ("Ioana", "182000", "does not match", "attempts"):
            assert fragment not in body
    assert store.redacted()
    assert store.redactions == 1


@pytest.mark.parametrize(("door", "turn", "ends", "ended"), [
    ((200, ACCEPTED), "alone", "case_step_ended", []),
    ((200, ACCEPTED), "halted", "tool_loop_halt", []),
    ((403, {"code": "forbidden", "message": "Not this agent's step."}), "halted", "tool_loop_halt",
     [{"reason": "failed", "detail": "keel-403", "usage": USAGE, "bundle_version": None}]),
    ((200, ACCEPTED), "answered", "completed", []),
], ids=["accepted", "accepted-then-halted", "stopped-then-halted", "accepted-then-answered"])
async def test_a_step_ended_turn_reports_nothing(fake_keel, monkeypatch, store, door, turn, ends, ended):
    fake_keel.answer(*door)
    replies = [_calls("complete_step", COMPLETION)]
    if turn == "halted":
        # A call in the report's batch that halts the turn on its first failure.
        store.row.config["tool_loop_guardrails"] = {"hard_stop_enabled": True, "hard_stop_after": {"same_tool_failure": 1}}
        replies = [_batch(("complete_step", COMPLETION, "c1"), ("read_file", {"path": "in/missing.pdf"}, "c2"))]
    elif turn == "answered":
        # A person writes while Keel takes the report: the turn goes on, and the model answers in words.
        fake_keel.during = lambda: store.write(EventType.USER_MESSAGE, {"content": "Did you check the co-applicant?"})
        replies.append(_says("Yes: the co-applicant's payslips are in the report."))

    sent = await _wake(
        monkeypatch, store, replies, sandbox={"read_file": json.dumps({"error": "No such file"})},
        # The guardrail watches a streamed answer's calls, as a worker runs them.
        streaming=turn == "halted",
    )

    assert len(sent) == len(replies)
    assert NUDGE not in json.dumps(sent)
    assert _ended_with(store) == ends
    assert len(fake_keel.bodies("/complete")) == 1
    # Only what the report itself told Keel.
    assert fake_keel.bodies("/ended") == ended
    assert store.redactions == 1


async def test_an_ordinary_session_is_never_nudged(fake_keel, monkeypatch, tmp_path):
    ordinary = _step({})
    ordinary.config = {}
    store = _StepStore(ordinary, LocalBackend(str(tmp_path)))

    sent = await _wake(monkeypatch, store, [_says("They show a monthly income of 5200.")])

    assert len(sent) == 1
    assert NUDGE not in json.dumps(sent[0]["messages"])
    assert _ended_with(store) == "completed"
    assert store.row.status == "completed"
    assert fake_keel.requests == []


@pytest.mark.parametrize("keel", ["unreachable", "no-link"])
@pytest.mark.parametrize("end", ["no-outcome", "failed"])
async def test_keel_down_does_not_stop_the_turn_ending(fake_keel, monkeypatch, store, caplog, keel, end):
    if keel == "unreachable":
        fake_keel.unreachable()
    else:
        use_keel_client(None)
    replies = [_says("I read the checklist."), _says("It looks complete.")] if end == "no-outcome" else [_cut_off()] * 3

    with caplog.at_level(logging.WARNING):
        await _wake(monkeypatch, store, replies)

    if end == "no-outcome":
        assert _ended_with(store) == "completed"
        assert store.row.status == "completed"
        assert not store.redacted()
    else:
        assert _failed_with(store) == "provider_error"
        assert store.row.status == "failed"
        # The step is over for the session whether or not Keel heard.
        assert store.redacted()
    assert len(fake_keel.bodies("/ended")) == (1 if keel == "unreachable" else 0)
    # Said by the session's id, and with nothing of the case.
    told = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "Keel" in r.getMessage()]
    assert any(str(store.row.id) in line for line in told)
    assert not any("Ioana" in line or "182000" in line for line in told)


@pytest.mark.parametrize("end", ["no-outcome", "failed", "given-up"])
async def test_a_keel_that_never_answers_holds_a_steps_end_briefly(monkeypatch, tmp_path, caplog, end):
    # Keel's door takes the request and never answers: the turn's end, its lease and its worker slot wait
    # for Keel no longer than telling it how the step ended may take.
    monkeypatch.setattr(keel_client_module, "ENDED_TIMEOUT_SECONDS", 0.1)
    told: list[httpx.Request] = []

    async def silent(request: httpx.Request) -> httpx.Response:
        told.append(request)
        await asyncio.Event().wait()
        raise AssertionError("a silent door never answers")

    previous = keel_client()
    use_keel_client(KeelClient("http://keel-door.test", "door-key", transport=httpx.MockTransport(silent)))
    store = _SweptStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING):
            # Bounded here too, so an end held without end fails rather than hangs.
            if end == "no-outcome":
                await asyncio.wait_for(_wake(monkeypatch, store, [_says("I read it."), _says("It looks complete.")]), 5)
            elif end == "failed":
                await asyncio.wait_for(_wake(monkeypatch, store, [_cut_off()] * 3), 5)
            else:
                await asyncio.wait_for(
                    _orchestrator(store, None)._abandon_unrecoverable_session(store.row, attempts=3, reason="no progress"), 5,
                )
    finally:
        use_keel_client(previous)

    assert time.monotonic() - started < 2
    assert [json.loads(r.content)["reason"] for r in told] == ["no-outcome" if end == "no-outcome" else "failed"]
    if end == "no-outcome":
        assert _ended_with(store) == "completed"
        assert not store.redacted()
    else:
        # The step is over for the session whether or not Keel heard.
        assert store.row.status == "failed"
        assert store.redacted()
    assert any(str(store.row.id) in r.getMessage() for r in caplog.records if "Keel" in r.getMessage())


class _ProviderRefused(Exception):
    """A model provider refusing the request, quoting it."""


def _orchestrator(store: _StepStore, harness) -> Orchestrator:
    return Orchestrator(
        redis_client=FakeRedis(), session_store=store, harness_factory=lambda _sid: harness,
        agent_id=store.row.agent_id, queue_key="work", max_concurrent=1,
    )


def _ordinary(tmp_path) -> _StepStore:
    ordinary = _step({})
    ordinary.config = {}
    return _StepStore(ordinary, LocalBackend(str(tmp_path)))


@pytest.mark.parametrize(("session", "crashes", "code"), [
    ("step", "same", "crash_loop_detected"),
    ("step", "varied", "max_retries_exhausted"),
    ("completed-step", "same", None),
    ("ordinary", "same", None),
], ids=["crash-loop", "retries-exhausted", "step-already-over", "ordinary"])
async def test_a_session_the_dispatcher_fails_is_reported_as_failed(
    fake_keel, monkeypatch, tmp_path, session, crashes, code,
):
    monkeypatch.setattr("surogates.orchestrator.dispatcher._BASE_RETRY_DELAY", 0)
    store = _ordinary(tmp_path) if session == "ordinary" else _StepStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))
    if session == "completed-step":
        # Keel took the step's report in an earlier wake, which removed its inputs.
        await store.redact_case_inputs(store.row.id)
    if crashes == "same":
        refusals = [_ProviderRefused("rejected: Ioana declared 182000.00") for _ in range(3)]
    else:
        refusals = [
            kind(f"rejected: Ioana declared 182000.00 ({n})")
            for n, kind in enumerate((_ProviderRefused, TimeoutError, RuntimeError))
        ]

    class Worker:
        async def wake(self, _session_id):
            # The model's call raises, and the turn crashes: no turn end of its own.
            await _wake(monkeypatch, store, [refusals.pop(0)])

    await _orchestrator(store, Worker())._process(store.row.id)

    assert refusals == []
    assert _failed_with(store) == (code or "crash_loop_detected")
    if code is None:
        assert fake_keel.requests == []
        assert store.redactions == (1 if session == "completed-step" else 0)
        return
    assert fake_keel.bodies("/ended") == [
        {"reason": "failed", "detail": code, "usage": USAGE, "bundle_version": None},
    ]
    for request in fake_keel.requests:
        for fragment in ("Ioana", "182000", "rejected", "Refused"):
            assert fragment not in request.content.decode()
    assert store.redacted()
    assert store.redactions == 1


class _SweptStore(_StepStore):
    """The step's store as the orphan sweeper uses it."""

    async def release_stale_lease(self, session_id) -> None:
        pass

    async def fail_if_active(self, session_id) -> bool:
        self.row.status = "failed"
        return True


async def test_a_step_the_sweeper_gives_up_is_reported_as_failed(fake_keel, tmp_path):
    store = _SweptStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))

    await _orchestrator(store, None)._abandon_unrecoverable_session(store.row, attempts=3, reason="no progress")

    assert _failed_with(store) == "recovery_loop"
    assert fake_keel.bodies("/ended") == [
        {"reason": "failed", "detail": "recovery_loop", "usage": USAGE, "bundle_version": None},
    ]
    assert store.redacted()


QUESTION = {"question": "Which income counts?"}
FOLLOW_UP = "Did you check the co-applicant?"
REPORTS = {
    "drafted": (DRAFTED, "complete_step", COMPLETION),
    "hand-raised": (HAND_RAISED, "raise_hand", QUESTION),
    "completed": (ACCEPTED, "complete_step", COMPLETION),
}


def _a_person_writes_during_the_report(fake_keel, store) -> None:
    """A person's message lands while Keel takes the report: the turn goes on past it."""

    def write() -> None:
        if len(fake_keel.requests) == 1:
            store.write(EventType.USER_MESSAGE, {"content": FOLLOW_UP})

    fake_keel.during = write


@pytest.mark.parametrize("result", ["drafted", "hand-raised", "completed"])
async def test_a_reported_step_whose_wake_then_crashes_is_not_failed_by_the_dispatcher(
    fake_keel, monkeypatch, store, result,
):
    monkeypatch.setattr("surogates.orchestrator.dispatcher._BASE_RETRY_DELAY", 0)
    answer, tool, arguments = REPORTS[result]
    fake_keel.answer(200, answer)
    _a_person_writes_during_the_report(fake_keel, store)
    wakes = [
        [_calls(tool, arguments), _ProviderRefused("rejected: Ioana declared 182000.00")],
        [_ProviderRefused("rejected: Ioana declared 182000.00")],
        [_ProviderRefused("rejected: Ioana declared 182000.00")],
    ]

    class Worker:
        async def wake(self, _session_id):
            replies = wakes.pop(0)
            # The first wake reports, then crashes on the person's message; its retries crash on it again.
            opening = None if len(replies) == 2 else (EventType.USER_MESSAGE, {"content": FOLLOW_UP}, FOLLOW_UP)
            await _wake(monkeypatch, store, replies, opening=opening)

    await _orchestrator(store, Worker())._process(store.row.id)

    assert wakes == []
    assert _failed_with(store) == "crash_loop_detected"
    assert fake_keel.bodies("/ended") == []
    assert store.reported() == result
    # A draft or a raised hand keeps its inputs: the reviewer's changes or the person's answer resume it here.
    assert store.redacted() is (result == "completed")
    assert store.redactions == (1 if result == "completed" else 0)


@pytest.mark.parametrize("result", ["drafted", "hand-raised"])
async def test_a_failure_in_the_wake_of_a_draft_or_a_raised_hand_reports_nothing(fake_keel, monkeypatch, store, result):
    answer, tool, arguments = REPORTS[result]
    fake_keel.answer(200, answer)
    _a_person_writes_during_the_report(fake_keel, store)

    await _wake(monkeypatch, store, [_calls(tool, arguments), _cut_off(), _cut_off(), _cut_off()])

    assert _failed_with(store) == "provider_error"
    assert fake_keel.bodies("/ended") == []
    assert store.reported() == result
    assert not store.redacted()
    assert store.redactions == 0


async def test_a_hand_raised_step_woken_by_a_helper_is_neither_nudged_nor_reported(fake_keel, monkeypatch, store):
    await store.mark_case_step_reported(store.row.id, "hand-raised")
    helper_done = "A helper you started finished: the payslips are summarized."

    sent = await _wake(
        monkeypatch, store, [_says("Noted: the payslips are summarized."), _says("Still waiting on the answer.")],
        opening=(EventType.WORKER_COMPLETE, {"worker_session_id": str(uuid4()), "result": helper_done}, helper_done),
    )

    assert len(sent) == 1
    assert NUDGE not in json.dumps(sent)
    assert _ended_with(store) == "completed"
    assert fake_keel.requests == []
    assert store.reported() == "hand-raised"
    assert not store.redacted()


async def test_a_tool_call_between_two_answers_does_not_rearm_the_nudge(fake_keel, monkeypatch, store):
    sent = await _wake(monkeypatch, store, [
        _says("I read the checklist."),
        _calls("read_file", {"path": "in/checklist.pdf"}, "c1"),
        _says("The checklist is complete."),
    ], sandbox={"read_file": "Checklist: all documents present."})

    assert len(sent) == 3
    assert json.dumps(sent[2]["messages"]).count("Finish it now") == 1
    assert _ended_with(store) == "completed"
    assert fake_keel.bodies("/ended") == [
        {"reason": "no-outcome", "detail": "completed", "usage": USAGE, "bundle_version": None},
    ]


async def test_a_child_of_a_step_is_never_nudged_or_reported(fake_keel, monkeypatch, tmp_path):
    # Even one whose config carried the step's contract over: only the step's own session is the step.
    child = _step(deepcopy(CONTRACT))
    child.parent_id = uuid4()
    store = _StepStore(child, LocalBackend(str(tmp_path)))
    task = "Summarize the payslips in /workspace/in."

    sent = await _wake(
        monkeypatch, store, [_says("The payslips show 5200 a month."), _says("Done.")],
        opening=(EventType.USER_MESSAGE, {"content": task}, task),
    )

    assert len(sent) == 1
    assert NUDGE not in json.dumps(sent)
    # Its own end; its parent hears of it after.
    assert [d["reason"] for sid, kind, d in store.events if sid == child.id and kind is EventType.SESSION_COMPLETE] == ["completed"]
    assert fake_keel.requests == []
    assert not store.redacted()


@pytest.mark.parametrize("report", ["drafted", "hand-raised", "no-outcome"])
async def test_keels_message_written_as_the_reporting_turn_ends_wakes_the_step(fake_keel, monkeypatch, tmp_path, report):
    store = _QueuedStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))
    _started(store)
    if report == "no-outcome":
        # Keel takes a turn with no outcome as a raised hand, and a person answers between Keel's 200 and the mark.
        answer, tool, arguments, kind, content = GIVEN_BACK["hand-raised"]
        first = [_says("I read the checklist."), _says("It looks complete.")]
        marking = store.mark_case_step_reported

        async def keel_answers_first(session_id, result, **guard):
            store.mark_case_step_reported = marking
            store.keel_writes(kind, content)
            await marking(session_id, result, **guard)

        store.mark_case_step_reported = keel_answers_first
        ending = None
    else:
        # The model's report ends the step; Keel's message lands as the turn's end detaches the sandbox.
        answer, tool, arguments, kind, content = GIVEN_BACK[report]
        first = [_calls(tool, arguments)]

        def ending() -> None:
            store.keel_writes(kind, content)

    fake_keel.answer(200, answer)

    await _queued_wake(monkeypatch, store, first, ending=ending)

    # At rest when the message landed, the step was resumed by it; still at work, it was not.
    assert store.row.status == ("active" if report == "no-outcome" else "completed")
    assert store.reported() is None
    assert store.cursor < store.message_of_keel(content).id

    # The wake Keel's message queued runs, and the model reads the message: the step is the session's again.
    sent = await _queued_wake(monkeypatch, store, [_calls(tool, arguments, "again")])

    assert len(sent) == 1
    resumes = [data["source"] for _, kind, data in store.events if kind is EventType.SESSION_RESUME]
    # Found at rest by its wake, the step is woken by the message all the same.
    assert resumes == ([] if report == "no-outcome" else ["stranded_user_message"])
    assert content in json.dumps(sent[0]["messages"])
    assert json.loads(fake_keel.requests[-1].content)["call_id"] == "again"
    assert _ended_with(store) == "case_step_ended"
    assert store.reported() == answer["result"]


async def test_a_turn_keel_takes_as_a_raised_hand_is_left_to_the_person(fake_keel, monkeypatch, store):
    fake_keel.answer(200, HAND_RAISED)
    await _wake(monkeypatch, store, [_says("I read the checklist."), _says("It looks complete.")])

    assert fake_keel.bodies("/ended") == [
        {"reason": "no-outcome", "detail": "completed", "usage": USAGE, "bundle_version": None},
    ]
    assert store.reported() == "hand-raised"
    assert not store.redacted()

    # A helper's report wakes the step before the person answers.
    helper_done = "A helper you started finished: the payslips are summarized."
    sent = await _wake(
        monkeypatch, store, [_says("Noted: the payslips are summarized."), _says("Still waiting on the answer.")],
        opening=(EventType.WORKER_COMPLETE, {"worker_session_id": str(uuid4()), "result": helper_done}, helper_done),
    )

    assert len(sent) == 1
    assert NUDGE not in json.dumps(sent)
    assert len(fake_keel.bodies("/ended")) == 1
