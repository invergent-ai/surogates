"""The two tools of an agent's session on a work item: ``complete_item`` and ``return_item``.

A case gives an agent a work item, a step to do, a vote to cast or a raised
hand to answer, and the item's session reports it to Keel's door:
``complete_item`` completes the step, casts the vote or answers the hand;
``return_item`` gives the item back, with a reason.  Keel's answer decides
what the session does next:

- a recorded report ends the item for the session, and the turn;
- a refusal is an error the model fixes, and the item stays the session's;
- an unreachable Keel is called again;
- a refusal for good stops the item, Keel is told it failed, and people
  take it.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from surogates.keel.client import keel_client, use_keel_client
from surogates.keel.items import ItemContract, render_item_kickoff
from surogates.keel.steps import CASE_ITEM_KEY, CASE_STEP_KEY
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.storage.backend import LocalBackend
from tests.test_case_step_isolation import CONTRACT
from tests.test_case_step_tools import (  # noqa: F401 - fake_keel is a fixture
    CASE,
    _calls,
    _ended_with,
    _FakeKeel,
    _registry,
    _step,
    _StepStore,
    _wake,
    fake_keel,
    kwargs,
)

#: Where in the loan case each item is, as Keel gives it.
_CASE = {key: CONTRACT[key] for key in ("case_id", "case_number", "case_type", "type_version", "token", "node", "step")}
STEP_ITEM = {
    **_CASE,
    "item_id": "item-step",
    "kind": "step",
    "inputs": {"applicant": "Ioana", "amount": "12000"},
    "form": {"type": "object", "properties": {"amount": {"type": "string"}}, "additionalProperties": False},
    "outcomes": {"approve": {"required": ["amount"]}, "refer": {"required": []}},
    "knowledge": ["kb-uuid"],
}
VOTE_ITEM = {
    **_CASE,
    "item_id": "item-vote",
    "kind": "vote",
    "inputs": {"applicant": "Ioana", "amount": "12000"},
    "form": {},
    "outcomes": {"for": {"required": []}, "against": {"required": []}},
}
HAND_ITEM = {
    **_CASE,
    "item_id": "item-hand",
    "kind": "raised_hand",
    "inputs": {"applicant": "Ioana"},
    "question": "The payslips name two employers. Which income counts?",
    "choices": ["Accept", "Refuse"],
    "evidence": [{"label": "Employer · March", "value": "the first one"}],
}
STEP_COMPLETION = {
    "outcome": "approve",
    "output": {"amount": "12000"},
    "sources": {"amount": [{"input": "amount"}]},
}
REASON = "The applicant's ID is missing."


def _item(contract: dict) -> Session:
    """An agent's session on the work item *contract*, made as a step's session is."""
    step = _step(contract)
    config = {CASE_ITEM_KEY if key == CASE_STEP_KEY else key: value for key, value in step.config.items()}
    return step.model_copy(update={"config": config})


def _store(tmp_path, contract: dict, **changes) -> _StepStore:
    return _StepStore(_item({**deepcopy(contract), **changes}), LocalBackend(str(tmp_path)))


async def _call(name: str, arguments: dict, store: _StepStore, tool_call_id: str = "c1", **more) -> str:
    return await _registry().dispatch(name, arguments, **kwargs(store, tool_call_id), **more)


def _item_route(store: _StepStore, route: str) -> str:
    return f"/api/harness/sessions/{store.row.id}/item/{route}"


async def test_a_completed_step_item_reaches_keel_and_ends_the_item(fake_keel: _FakeKeel, tmp_path):
    store = _store(tmp_path, STEP_ITEM)
    fake_keel.answer(200, {"recorded": True})

    result = await _call("complete_item", STEP_COMPLETION, store, tool_call_id="c3")

    [request] = fake_keel.requests
    assert request.method == "POST"
    assert request.url.path == _item_route(store, "complete")
    assert request.headers["authorization"] == "Bearer door-key"
    assert json.loads(request.content) == {**STEP_COMPLETION, "call_id": "c3"}
    assert result == "Item ended: Keel recorded it."
    assert store.reported() == "completed"
    assert store.redacted()


async def test_a_vote_item_casts_the_vote(fake_keel: _FakeKeel, tmp_path):
    store = _store(tmp_path, VOTE_ITEM)
    vote = {"outcome": "against", "output": {}, "sources": {}}

    result = await _call("complete_item", vote, store)

    assert fake_keel.bodies("/item/complete") == [{**vote, "call_id": "c1"}]
    assert result == "Item ended: Keel recorded it."
    assert store.reported() == "completed"

    # A vote is cast with one of its outcomes alone.
    assert "error" in json.loads(await _call("complete_item", {**vote, "outcome": "abstain"}, _store(tmp_path, VOTE_ITEM)))
    assert len(fake_keel.requests) == 1


async def test_a_raised_hand_item_answers_with_a_choice_or_in_words(fake_keel: _FakeKeel, tmp_path):
    chosen = _store(tmp_path, HAND_ITEM)
    result = await _call("complete_item", {"choice": "Accept", "note": "The March payslip is the latest."}, chosen, "c4")

    assert result == "Item ended: Keel recorded it."
    assert fake_keel.bodies("/item/complete") == [
        {"choice": "Accept", "note": "The March payslip is the latest.", "sources": [], "call_id": "c4"},
    ]
    assert chosen.reported() == "completed"

    in_words = _store(tmp_path, HAND_ITEM)
    sources = [{"file": "in/payslips/06.pdf"}]
    result = await _call("complete_item", {"answer": "Use June's payslip", "sources": sources}, in_words, "c5")

    assert result == "Item ended: Keel recorded it."
    assert fake_keel.bodies("/item/complete")[-1] == {
        "answer": "Use June's payslip", "note": None, "sources": sources, "call_id": "c5",
    }

    # A choice the hand does not offer, both a choice and an answer, neither, or sources that are no list of
    # objects never reach Keel.
    for arguments in (
        {"choice": "Maybe"},
        {"choice": "Accept", "answer": "Use June's payslip"},
        {"note": "No idea."},
        {"answer": "Use June's payslip", "sources": {"answer": [{"file": "in/payslips/06.pdf"}]}},
        {"answer": "Use June's payslip", "sources": ["in/payslips/06.pdf"]},
    ):
        result = await _call("complete_item", arguments, _store(tmp_path, HAND_ITEM))
        assert "error" in json.loads(result), arguments
    assert len(fake_keel.requests) == 2


async def test_a_file_field_of_a_step_item_becomes_a_case_file(fake_keel: _FakeKeel, tmp_path):
    store = _store(tmp_path, STEP_ITEM, files={"offer": "one"})
    offer = b"%PDF-1.7 offer"
    await store.put("out/offer.pdf", offer)
    completion = {**STEP_COMPLETION, "output": {**STEP_COMPLETION["output"], "offer": "/workspace/out/offer.pdf"}}

    result = await _call("complete_item", completion, store)

    assert result == "Item ended: Keel recorded it."
    [body] = fake_keel.bodies("/item/complete")
    assert body["output"] == {
        "amount": "12000",
        "offer": {
            "name": "offer.pdf", "uri": f"case-file://{CASE}/out/offer.pdf", "sha256": hashlib.sha256(offer).hexdigest(),
        },
    }

    # A path with no file behind it never reaches Keel.
    missing = {**completion, "output": {**completion["output"], "offer": "out/missing.pdf"}}
    error = json.loads(await _call("complete_item", missing, _store(tmp_path, STEP_ITEM, files={"offer": "one"})))
    assert error["error"] == "offer: no file out/missing.pdf in the workspace"
    assert len(fake_keel.requests) == 1


@pytest.mark.parametrize(("rejection", "shown"), [
    ({"code": "authority", "message": "Over your limit."}, "Over your limit."),
    (
        {"code": "missing-sources", "message": "No source for amount.", "attempts_left": 1},
        "No source for amount. (1 attempt left; fix the report and call complete_item again, or call return_item)",
    ),
], ids=["uncounted", "counted"])
async def test_keels_refusal_comes_back_to_fix(fake_keel: _FakeKeel, tmp_path, rejection, shown):
    store = _store(tmp_path, STEP_ITEM)
    fake_keel.answer(409, rejection)
    fake_keel.answer(200, {"recorded": True})

    refused = json.loads(await _call("complete_item", STEP_COMPLETION, store, "c1"))

    assert refused == {"error": shown, "code": rejection["code"]}
    assert store.reported() is None
    assert not store.redacted()

    result = await _call("complete_item", {**STEP_COMPLETION, "outcome": "refer"}, store, "c2")

    assert result == "Item ended: Keel recorded it."
    assert len(fake_keel.bodies("/item/complete")) == 2


@pytest.mark.parametrize("door", [None, (503, {"code": "unavailable", "message": "Try again."})], ids=["unreachable", "503"])
async def test_keel_unreachable_asks_to_call_again(fake_keel: _FakeKeel, tmp_path, door):
    store = _store(tmp_path, STEP_ITEM)
    if door is None:
        fake_keel.unreachable()
    else:
        fake_keel.answer(*door)

    result = json.loads(await _call("complete_item", STEP_COMPLETION, store))

    assert result == {"error": "Keel could not be reached; call complete_item again."}
    assert fake_keel.bodies("/item/ended") == []
    assert store.reported() is None
    assert not store.redacted()


async def test_an_item_keel_no_longer_runs_ends_unmarked(fake_keel: _FakeKeel, tmp_path):
    store = _store(tmp_path, STEP_ITEM)
    fake_keel.answer(409, {"code": "not-running", "message": "The item has moved on."})

    result = await _call("return_item", {"reason": REASON}, store)

    assert result == "Item ended: the item is no longer this session's."
    assert store.reported() is None
    assert fake_keel.bodies("/item/ended") == []


@pytest.mark.parametrize("door", [
    (404, {"code": "not-found", "message": "No such item run."}),
    (503, {"code": "harness-off", "message": "The door is off."}),
], ids=["404", "harness-off"])
async def test_a_refusal_for_good_stops_the_item(fake_keel: _FakeKeel, tmp_path, door):
    store = _store(tmp_path, STEP_ITEM)
    fake_keel.answer(*door)

    result = await _call("complete_item", STEP_COMPLETION, store)

    status = door[0]
    assert result == f"Item stopped: Keel refused this item's report ({status}); people take the item."
    assert [r.url.path for r in fake_keel.requests] == [_item_route(store, "complete"), _item_route(store, "ended")]
    assert fake_keel.bodies("/item/ended") == [{"status": "failed", "detail": f"keel-{status}"}]
    assert store.reported() == "stopped"
    assert store.redacted()


async def test_a_runtime_without_keel_stops_the_item(tmp_path):
    store = _store(tmp_path, HAND_ITEM)
    previous = keel_client()
    use_keel_client(None)
    try:
        result = await _call("return_item", {"reason": REASON}, store)
    finally:
        use_keel_client(previous)

    assert result == "Item stopped: this runtime has no link to Keel; people take the item."
    assert store.reported() == "stopped"
    assert store.redacted()


async def test_return_item_gives_the_item_back_with_its_reason(fake_keel: _FakeKeel, tmp_path):
    store = _store(tmp_path, VOTE_ITEM)

    result = await _call("return_item", {"reason": f"  {REASON}  "}, store, "c6")

    assert fake_keel.bodies("/item/return") == [{"reason": REASON, "call_id": "c6"}]
    assert result == "Item ended: the item went back."
    assert store.reported() == "returned"
    assert store.redacted()

    for reason in ("", "   ", "x" * 2001, None):
        assert "error" in json.loads(await _call("return_item", {"reason": reason}, _store(tmp_path, VOTE_ITEM))), reason
    assert len(fake_keel.requests) == 1


async def test_a_second_report_in_the_wake_never_reaches_keel(fake_keel: _FakeKeel, tmp_path):
    store = _store(tmp_path, STEP_ITEM)
    lease = uuid4()

    first = await _call("complete_item", STEP_COMPLETION, store, "c1", lease_token=lease)
    second = await _call("return_item", {"reason": REASON}, store, "c2", lease_token=lease)

    assert first == "Item ended: Keel recorded it."
    assert second == "Item ended: the item is no longer this session's."
    assert len(fake_keel.requests) == 1
    assert store.reported() == "completed"


async def test_only_the_items_own_session_can_report_it(fake_keel: _FakeKeel, tmp_path):
    step = _StepStore(_step(deepcopy(CONTRACT)), LocalBackend(str(tmp_path)))
    helper_config = {k: v for k, v in _store(tmp_path, STEP_ITEM).row.config.items() if k != CASE_ITEM_KEY}

    for config in (step.row.config, helper_config):
        for name, arguments in (("complete_item", STEP_COMPLETION), ("return_item", {"reason": REASON})):
            result = await _registry().dispatch(name, arguments, **{**kwargs(step), "session_config": config})
            assert "error" in json.loads(result), name
    assert fake_keel.requests == []


@pytest.mark.parametrize(("contract", "call"), [
    (STEP_ITEM, ("complete_item", STEP_COMPLETION)),
    (HAND_ITEM, ("return_item", {"reason": REASON})),
    ({**HAND_ITEM, "choices": None}, ("complete_item", {"answer": "Use June's payslip"})),
], ids=["step", "raised-hand", "raised-hand-without-choices"])
async def test_the_schema_offers_the_items_outcomes_or_choices(fake_keel: _FakeKeel, monkeypatch, tmp_path, contract, call):
    store = _store(tmp_path, contract)
    kickoff = render_item_kickoff(ItemContract.model_validate(contract))

    [request] = await _wake(
        monkeypatch, store, [_calls(*call)],
        opening=(EventType.USER_MESSAGE, {"content": kickoff, "synthetic": "keel_item"}, kickoff),
    )

    tools = {t["function"]["name"]: t["function"] for t in request["tools"]}
    assert "return_item" in tools
    assert tools.keys().isdisjoint({"complete_step", "raise_hand"})
    properties = tools["complete_item"]["parameters"]["properties"]
    if contract["kind"] == "step":
        assert set(properties) == {"outcome", "output", "sources"}
        assert properties["outcome"]["enum"] == ["approve", "refer"]
        assert properties["output"]["properties"] == {"amount": {"type": "string"}}
        assert set(properties["sources"]["properties"]) == {"amount"}
    elif contract["choices"]:
        assert set(properties) == {"choice", "answer", "note", "sources"}
        assert properties["choice"]["enum"] == ["Accept", "Refuse"]
        assert properties["sources"]["type"] == "array"
        assert not tools["complete_item"]["parameters"].get("required")
    else:
        assert set(properties) == {"answer", "note", "sources"}
    # Its report ends the item, and the turn with it.
    assert _ended_with(store) == "case_step_ended"
    assert store.reported() in ("completed", "returned")
