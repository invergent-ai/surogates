"""An item session's turn that ends without a report, and one that fails.

A work item ends only with ``complete_item`` or ``return_item``.  A turn of
the item's own session that ends in words is asked once to make the report,
and goes on.  One that still ends without it is told to Keel through the
item's door as ``no-outcome``, with the turn end's code: Keel gives the item
back, and once it took that the item is over for the session.  A turn that
fails, and a session the dispatcher gives up on, is told as ``failed``, with
the failure's code and never its text, and the item's inputs leave the
session whatever Keel answered.

An item that reported is never told again, a helper of the item is never
the item, and a report of a response that is later dropped never reaches
Keel.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.keel.items import ItemContract, render_item_kickoff
from surogates.session.events import EventType
from surogates.storage.backend import LocalBackend
from tests.test_case_item_tools import STEP_COMPLETION, STEP_ITEM, _item, _store
from tests.test_case_step_tools import (  # noqa: F401 - fake_keel is a fixture
    _calls,
    _ended_with,
    _queued_wake,
    _QueuedStore,
    _step_harness,
    _StepStore,
    _wake,
    fake_keel,
)
from tests.test_case_step_turn_end import (
    _cut_off,
    _failed_with,
    _last_user_message,
    _orchestrator,
    _ProviderRefused,
    _says,
)

NUDGE = (
    "This session works one work item of a case. Finish it now: call complete_item, "
    "or return_item with your reason if you cannot."
)
RECORDED = {"recorded": True}
FOLLOW_UP = "Did you check the co-applicant?"
# What leaks if a failure's text, or the case's data, reaches Keel.
LEAKS = ("Ioana", "182000", "does not match", "rejected", "Refused")


def _kicked_off(store: _StepStore) -> str:
    """Keel starts the item's session: its kickoff, in the session's log, and the message the model reads of it."""
    kickoff = render_item_kickoff(ItemContract.model_validate(store._contract()))
    store.write(EventType.USER_MESSAGE, {"content": kickoff, "synthetic": "keel_item"})
    return kickoff


async def _item_wake(monkeypatch, store: _StepStore, replies: list, **more) -> list[dict]:
    """One wake of the item's session, opened by Keel's kickoff: the requests the model was sent."""
    kickoff = _kicked_off(store)
    return await _wake(monkeypatch, store, replies, opening=(EventType.USER_MESSAGE, None, kickoff), **more)


def _no_case_data_reached(fake_keel) -> None:
    for request in fake_keel.requests:
        body = request.content.decode()
        for fragment in LEAKS:
            assert fragment not in body


@pytest.mark.parametrize("keel", ["takes-it", "unreachable"])
async def test_an_item_turn_that_ends_in_words_is_nudged_once_then_given_back(fake_keel, monkeypatch, tmp_path, keel):
    store = _store(tmp_path, STEP_ITEM)
    if keel == "unreachable":
        fake_keel.unreachable()

    sent = await _item_wake(monkeypatch, store, [
        _says("I read the payslips."),
        _says("The amount looks right to me."),
    ])

    assert len(sent) == 2
    assert _last_user_message(sent[1]) == NUDGE
    assert json.dumps(sent[1]["messages"]).count("Finish it now") == 1
    # Kept in the item's log as the harness's own message.
    [nudge] = [e.data for e in store.log if e.type == EventType.USER_MESSAGE.value and e.data["content"] == NUDGE]
    assert nudge["synthetic"] == "keel_step_nudge"
    assert _ended_with(store) == "completed"
    assert fake_keel.bodies("/item/ended") == [{"status": "no-outcome", "detail": "completed"}]
    assert len(fake_keel.requests) == 1
    if keel == "takes-it":
        # Keel gave the item back: it is over for the session.
        assert store.reported() == "returned"
        assert store.redacted()
    else:
        # Keel never heard: the item is still the session's, and the turn ended all the same.
        assert store.reported() is None
        assert not store.redacted()


@pytest.mark.parametrize("keel", ["takes-it", "unreachable"])
async def test_an_item_turn_that_fails_tells_keel_and_ends_the_item(fake_keel, monkeypatch, tmp_path, keel):
    store = _store(tmp_path, STEP_ITEM)
    if keel == "unreachable":
        fake_keel.unreachable()

    sent = await _item_wake(monkeypatch, store, [_cut_off()] * 3)

    assert len(sent) == 3
    assert _failed_with(store) == "provider_error"
    assert fake_keel.bodies("/item/ended") == [{"status": "failed", "detail": "provider_error"}]
    _no_case_data_reached(fake_keel)
    # The item is over for the session whatever Keel answered.
    assert store.redacted()
    assert store.redactions == 1


async def test_an_item_that_reported_is_never_told_again(fake_keel, monkeypatch, tmp_path):
    store = _store(tmp_path, STEP_ITEM)
    fake_keel.answer(200, RECORDED)
    await _item_wake(monkeypatch, store, [_calls("complete_item", STEP_COMPLETION)])
    assert _ended_with(store) == "case_step_ended"
    assert store.reported() == "completed"
    follow_up = (EventType.USER_MESSAGE, {"content": FOLLOW_UP}, FOLLOW_UP)

    # A later wake that ends in words, then one that fails.
    words = await _wake(monkeypatch, store, [_says("The co-applicant has no income.")], opening=follow_up)
    assert len(words) == 1
    assert NUDGE not in json.dumps(words)
    assert _ended_with(store) == "completed"
    await _wake(monkeypatch, store, [_cut_off()] * 3, opening=follow_up)
    assert _failed_with(store) == "provider_error"

    assert [r.url.path for r in fake_keel.requests] == [f"/api/harness/sessions/{store.row.id}/item/complete"]
    assert store.reported() == "completed"
    assert store.redactions == 1


@pytest.mark.parametrize(("session", "crashes", "code"), [
    ("item", "same", "crash_loop_detected"),
    ("item", "varied", "max_retries_exhausted"),
    ("item-already-over", "same", None),
], ids=["crash-loop", "retries-exhausted", "item-already-over"])
async def test_an_item_the_dispatcher_gives_up_is_reported_as_failed(
    fake_keel, monkeypatch, tmp_path, session, crashes, code,
):
    monkeypatch.setattr("surogates.orchestrator.dispatcher._BASE_RETRY_DELAY", 0)
    store = _store(tmp_path, STEP_ITEM)
    if session == "item-already-over":
        # Keel recorded the item's report in an earlier wake, which removed its inputs.
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
            await _item_wake(monkeypatch, store, [refusals.pop(0)])

    await _orchestrator(store, Worker())._process(store.row.id)

    assert refusals == []
    assert _failed_with(store) == (code or "crash_loop_detected")
    if code is None:
        assert fake_keel.requests == []
        assert store.redactions == 1
        return
    assert fake_keel.bodies("/item/ended") == [{"status": "failed", "detail": code}]
    _no_case_data_reached(fake_keel)
    assert store.redacted()
    assert store.redactions == 1


@pytest.mark.parametrize("end", ["words", "fails"])
async def test_a_child_of_an_item_is_never_nudged_or_reported(fake_keel, monkeypatch, tmp_path, end):
    # Even one whose config carried the item's contract over: only the item's own session is the item.
    child = _item(deepcopy(STEP_ITEM))
    child.parent_id = uuid4()
    store = _StepStore(child, LocalBackend(str(tmp_path)))
    task = "Summarize the payslips in /workspace/in."
    replies = [_says("The payslips show 5200 a month."), _says("Done.")] if end == "words" else [_cut_off()] * 3

    sent = await _wake(monkeypatch, store, replies, opening=(EventType.USER_MESSAGE, {"content": task}, task))

    assert NUDGE not in json.dumps(sent)
    assert len(sent) == (1 if end == "words" else 3)
    # Its own end; its parent hears of it after.
    ends = [
        (kind, data["reason"]) for sid, kind, data in store.events
        if sid == child.id and kind in (EventType.SESSION_COMPLETE, EventType.SESSION_FAIL)
    ]
    assert ends == (
        [(EventType.SESSION_COMPLETE, "completed")] if end == "words" else [(EventType.SESSION_FAIL, "provider_error")]
    )
    assert fake_keel.requests == []
    assert not store.redacted()


async def test_an_item_report_of_a_dropped_response_never_reaches_keel(fake_keel, monkeypatch, tmp_path):
    store = _store(tmp_path, STEP_ITEM)
    fake_keel.answer(200, RECORDED)
    [dropped] = _calls("complete_item", STEP_COMPLETION, "dropped")[0]["tool_calls"]
    kept = _calls("complete_item", STEP_COMPLETION, "kept")
    sent: list[dict] = []
    harness = _step_harness(monkeypatch, store, [], sent, streaming=True)

    async def llm(**call):
        sent.append(call["create_kwargs"])
        # The stream announces a report, then breaks; the provider's retry streams the answer the turn keeps.
        call["on_tool_call_complete"](dropped)
        await asyncio.sleep(0.05)
        call["on_stream_retry"]()(kept[0]["tool_calls"][0])
        await asyncio.sleep(0.05)
        return kept

    monkeypatch.setattr(loop_module, "call_llm_with_retry", llm)
    kickoff = _kicked_off(store)
    await harness._run_loop(
        store.row.model_copy(deep=True), [{"role": "user", "content": kickoff}], "system",
        SimpleNamespace(lease_token=uuid4()), all_events=list(store.log),
    )

    assert len(sent) == 1
    assert [body["call_id"] for body in fake_keel.bodies("/item/complete")] == ["kept"]
    assert _ended_with(store) == "case_step_ended"
    assert store.reported() == "completed"


async def test_an_item_that_reported_is_not_woken_by_the_late_report_of_a_worker_it_spawned(
    fake_keel, monkeypatch, tmp_path,
):
    # The item ended where Keel recorded its report: a turn of its own after that would be no item's.
    store = _QueuedStore(_item(deepcopy(STEP_ITEM)), LocalBackend(str(tmp_path)))
    fake_keel.answer(200, RECORDED)
    _kicked_off(store)
    worker = str(uuid4())
    store.write(EventType.WORKER_SPAWNED, {"worker_id": worker, "goal": "Read the payslips."})
    assert len(await _queued_wake(monkeypatch, store, [_calls("complete_item", STEP_COMPLETION)])) == 1
    assert (_ended_with(store), store.row.status) == ("case_step_ended", "completed")

    store.write(EventType.WORKER_COMPLETE, {"worker_id": worker, "result": "Income is 5200."})
    assert await _queued_wake(monkeypatch, store, []) == []
    assert store.row.status == "completed"
