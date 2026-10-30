"""The AI plays a person: it writes first, waits for the agent, answers, takes over and hands back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import respx

from surogates.rehearsal import actor, store
from tests.test_rehearsal_api import Settings, _client
from tests.test_rehearsal_models import rsf  # noqa: F401


class _LLM:
    async def close(self):
        pass


@pytest.mark.asyncio
async def test_ai_person_writes_waits_answers_and_hands_over(rsf, monkeypatch):  # noqa: F811
    org_id, client = await _client(rsf)
    org = {"org_id": str(org_id)}
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "persona": "Ana, 60, back pain for a week, wants an appointment next week.",
    })
    assert r.status_code == 201
    run_json = r.json()
    person_json = run_json["people"][0]
    assert person_json["driver"] == "ai" and person_json["ai_state"] == "talking"

    moves = [actor.Move(action="write", text="Bună ziua, am dureri de spate"),
             actor.Move(action="done", note="Got an appointment")]
    prompts = []

    async def fake_generate(**kw):
        prompts.append(kw["messages"])
        return moves.pop(0)

    monkeypatch.setattr(actor, "generate_structured", fake_generate)
    monkeypatch.setattr(actor, "_client", lambda settings: (_LLM(), "m"))
    ai = actor.Actor(SimpleNamespace(session_factory=rsf, settings=Settings, http=httpx.AsyncClient()))

    [(run, person)] = await store.ai_people(rsf)
    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/whatsapp/{run.phone_number_id}").mock(
            return_value=httpx.Response(200))
        await ai._move(run, person)
    assert "dureri de spate" in hook.calls[0].request.content.decode()
    assert "back pain" in prompts[0][0]["content"]

    # Its turn is over until the agent answers.
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    assert len(prompts) == 1

    # The agent answers; once it has been quiet a moment the person moves again and is done.
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="out", kind="text",
                            body={"type": "text", "text": {"body": "Vă pot programa marți."}},
                            wamid="wamid.out1")
    monkeypatch.setattr(actor, "QUIET", timedelta(0))
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    assert "Vă pot programa marți" in prompts[1][1]["content"]
    assert await store.ai_people(rsf) == []
    [p] = await store.people(rsf, run.id)
    assert p.ai_state == "done" and p.ai_note == "Got an appointment"

    # Taking over hands the chat to the user; handing back restarts the AI.
    base = f"/internal/runs/{run.id}/people/{p.id}/driver"
    r = await client.put(base, params=org, json={"driver": "human"})
    assert r.json()["people"][0]["driver"] == "human"
    r = await client.put(base, params=org, json={"driver": "ai"})
    assert r.json()["people"][0]["ai_state"] == "talking"
    assert len(await store.ai_people(rsf)) == 1


def test_silent_agent_ends_the_part():
    now = datetime.now(timezone.utc)
    person = SimpleNamespace(ai_acted_at=now - timedelta(minutes=4), agent_typing_at=None)
    assert actor._waiting(person, [], now) == "no_reply"
    person.ai_acted_at = now - timedelta(seconds=30)
    assert actor._waiting(person, [], now) == "agent"
    person.ai_acted_at = None
    assert actor._waiting(person, [], now) is None


@pytest.mark.asyncio
async def test_the_ai_only_writes_on_chat_channels(rsf):  # noqa: F811
    org_id, client = await _client(rsf)
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1", "person_name": "Ana",
        "persona": "Ana", "channel": "voice",
    })
    assert r.status_code == 422
