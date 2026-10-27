import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop import _format_loop_list
from surogates.session.acting_principal import ActingPrincipal
from surogates.session.events import EventType
from surogates.tools.builtin.cron import (
    _cron_create_handler,
    _cron_delete_handler,
    _cron_get_handler,
    _cron_list_handler,
    _cron_pause_handler,
    _cron_update_handler,
)
from surogates.tools.loader import ResourceLoader

QUESTION = SimpleNamespace(id=6, type=EventType.TOOL_CALL.value, data={
    "name": "ask_user_question", "tool_call_id": "call-q",
    "arguments": {"questions": [{
        "prompt": "Programez asta?",
        "choices": [{"label": "Da, programează"}, {"label": "Nu"}],
    }]},
})


def _answer(label: str, *, other: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=7, type=EventType.ASK_USER_QUESTION_RESPONSE.value, data={
        "tool_call_id": "call-q",
        "responses": [{"question": "Programez asta?", "answer": label, "is_other": other}],
    })


YES = _answer("Da, programează")
SAID = SimpleNamespace(id=9, type=EventType.USER_MESSAGE.value, data={"content": "and one more"})


def _row(**fields):
    return SimpleNamespace(**{
        "id": uuid4(), "name": "Deploy check", "prompt": "check deploy", "schedule": {},
        "schedule_display": "Every 10 minutes", "timezone": "UTC", "status": "active",
        "next_run_at": None, "run_count": 0, "last_run_at": None, "last_error": None,
        "expires_at": None, "ended_reason": None, "kind": "interval", "cadence": {},
        "last_session_id": None, "paused_at": None, "ended_by": None, "created_from_session_id": None,
        **fields,
    })


class FakeStore:
    def __init__(self, row=None):
        self.created = []
        self.calls = []
        self.row = row or _row()

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return _row(
            name=kwargs["name"], prompt=kwargs["prompt"], schedule_display=kwargs["schedule"].display,
            timezone=kwargs["schedule"].timezone_name,
            next_run_at=datetime(2026, 10, 14, 5, 30, tzinfo=timezone.utc),
        )

    async def list_for_user(self, **kwargs):
        self.calls.append(("list", kwargs))
        return [self.row]

    async def get_for(self, schedule_id, **kwargs):
        self.calls.append(("get", kwargs))
        return self.row

    async def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return self.row

    async def cancel(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return True

    async def pause(self, **kwargs):
        self.calls.append(("pause", kwargs))
        return False


class FakeSessions:
    """The session's sender, and what was said, asked and answered in it."""

    def __init__(self, principal: ActingPrincipal, events: list | None = None):
        self.principal = principal
        self.events = [QUESTION, YES] if events is None else events

    async def resolve_acting_principal(self, session_id):
        return self.principal

    async def get_events(self, session_id, *, types=None, **_):
        return [e for e in self.events if types is None or e.type in {t.value for t in types}]


USER = ActingPrincipal(user_id=uuid4(), service_account_id=None)
SERVICE_ACCOUNT = ActingPrincipal(user_id=None, service_account_id=uuid4())
DIGEST = {
    "cron": "30 8 * * 1-5",
    "timezone": "Europe/Bucharest",
    "prompt": "Trimite-mi un rezumat în cinci puncte al articolelor noi de pe arXiv despre agenți AI.",
}


def _kwargs(sessions: FakeSessions, store: FakeStore, **extra) -> dict:
    # Managed channels hand the tool the agent's own service account as tenant;
    # the schedule is still the sender's.
    return {
        "tenant": SimpleNamespace(org_id=uuid4(), user_id=None, service_account_id=uuid4()),
        "agent_id": "agent-a",
        "session_id": str(uuid4()),
        "session_store": sessions,
        "scheduled_store": store,
        **extra,
    }


@pytest.mark.asyncio
async def test_cron_create_needs_a_sender_who_owns_it():
    result = json.loads(await _cron_create_handler(
        DIGEST, **_kwargs(FakeSessions(ActingPrincipal(user_id=None, service_account_id=None)), FakeStore()),
    ))
    assert result["success"] is False
    assert "signed-in user or a service account" in result["error"]


@pytest.mark.parametrize("events", [
    [], [SAID], [QUESTION, YES, SAID], [QUESTION, _answer("Nu")], [QUESTION, _answer("Da, programează", other=True)],
], ids=["never-asked", "said-something-else", "answered-before-the-last-message", "declined", "typed-its-own"])
@pytest.mark.asyncio
async def test_cron_create_makes_nothing_until_the_user_has_approved_it_this_turn(events):
    store = FakeStore()
    result = json.loads(await _cron_create_handler(DIGEST, **_kwargs(FakeSessions(USER, events), store)))
    assert result["success"] is False
    assert "ask_user_question" in result["error"]
    # What it would make, to put to the user.
    assert result["would"]["timezone"] == "Europe/Bucharest"
    assert result["would"]["next_run_at"].endswith(("+03:00", "+02:00"))
    assert result["would"]["runs"] == "until cancelled"
    assert store.created == []


@pytest.mark.asyncio
async def test_without_a_question_tool_an_operators_chat_makes_it_and_a_channel_cannot():
    offered = frozenset({"cron_create", "cron_list"})
    studio, whatsapp = FakeStore(), FakeStore()
    made = json.loads(await _cron_create_handler(
        DIGEST, **_kwargs(FakeSessions(SERVICE_ACCOUNT, []), studio, offered_tools=offered, channel="studio"),
    ))
    refused = json.loads(await _cron_create_handler(
        DIGEST, **_kwargs(FakeSessions(USER, []), whatsapp, offered_tools=offered, channel="whatsapp"),
    ))
    assert made["success"] is True and len(studio.created) == 1
    assert refused["success"] is False and "Studio" in refused["error"] and whatsapp.created == []


@pytest.mark.asyncio
async def test_cron_create_refuses_a_bad_schedule_before_asking():
    result = json.loads(await _cron_create_handler(
        {**DIGEST, "timezone": "Europe/Bucarest"}, **_kwargs(FakeSessions(USER, []), FakeStore()),
    ))
    assert result == {"success": False, "error": "Unknown timezone: Europe/Bucarest"}
    both = json.loads(await _cron_create_handler(
        {**DIGEST, "interval": "2h"}, **_kwargs(FakeSessions(USER, []), FakeStore()),
    ))
    assert both["success"] is False and "exactly one" in both["error"]


@pytest.mark.parametrize("principal", [USER, SERVICE_ACCOUNT], ids=["user", "service-account"])
@pytest.mark.asyncio
async def test_cron_create_once_approved_belongs_to_the_sender(principal):
    store = FakeStore()
    kwargs = _kwargs(FakeSessions(principal), store, session_config={"browser": {"profile_id": "p-1"}}, channel="studio")
    result = json.loads(await _cron_create_handler(DIGEST, **kwargs))

    assert result["success"] is True
    made = store.created[0]
    assert (made["user_id"], made["service_account_id"]) == (principal.user_id, principal.service_account_id)
    # It lasts: a weekday digest must not stop after a few days.
    assert (made["agent_id"], made["source"], made.get("expires_at")) == ("agent-a", "tool", None)
    assert str(made["created_from_session_id"]) == kwargs["session_id"]
    assert made["prompt"] == DIGEST["prompt"]
    # What its runs act with is kept on it.
    assert made["context"] == {"browser": {"profile_id": "p-1"}, "channel": "studio"}
    # Its next run on its own clock, to restate in any language.
    assert result["schedule"]["next_run_at"] == "2026-10-14T08:30:00+03:00"
    assert result["schedule"]["timezone"] == "Europe/Bucharest"


@pytest.mark.asyncio
async def test_a_one_off_is_one_instant_not_a_yearly_cron():
    store = FakeStore()
    await _cron_create_handler({**DIGEST, "recurring": False}, **_kwargs(FakeSessions(USER), store))
    await _cron_create_handler(
        {"at": "2099-01-02T15:30", "timezone": "Europe/Bucharest", "prompt": "remind me"},
        **_kwargs(FakeSessions(USER), store),
    )
    first, second = (made["schedule"] for made in store.created)
    assert first.kind == "once" and first.at.weekday() < 5
    assert second.kind == "once" and second.at == datetime(2099, 1, 2, 13, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_intervals_and_times_of_day_are_cadences_of_their_own():
    store = FakeStore()
    await _cron_create_handler({"interval": "25h", "prompt": "ping"}, **_kwargs(FakeSessions(USER), store))
    await _cron_create_handler(
        {"times": ["9:00", "17:30"], "days": ["mon", "wed"], "timezone": "Europe/Bucharest", "prompt": "digest"},
        **_kwargs(FakeSessions(USER), store),
    )
    interval, calendar = (made["schedule"] for made in store.created)
    assert (interval.kind, interval.seconds) == ("interval", 25 * 3600)
    assert (calendar.kind, calendar.days, calendar.times) == ("calendar", ("mon", "wed"), ("09:00", "17:30"))


@pytest.mark.parametrize("principal", [USER, SERVICE_ACCOUNT], ids=["user", "service-account"])
@pytest.mark.asyncio
async def test_list_get_and_cancel_ask_as_the_sender(principal):
    store = FakeStore()
    listed = json.loads(await _cron_list_handler({}, **_kwargs(FakeSessions(principal), store)))
    got = json.loads(await _cron_get_handler({"id": str(store.row.id)}, **_kwargs(FakeSessions(principal), store)))
    cancelled = json.loads(await _cron_delete_handler({"id": str(store.row.id)}, **_kwargs(FakeSessions(principal), store)))

    assert listed["schedules"][0]["name"] == "Deploy check"
    assert got["schedule"]["kind"] == "interval"
    assert cancelled["success"] is True
    for name, asked in store.calls:
        assert (asked["user_id"], asked["service_account_id"]) == (principal.user_id, principal.service_account_id)
    assert next(asked for name, asked in store.calls if name == "list")["status"] == "active,paused"
    assert next(asked for name, asked in store.calls if name == "cancel")["ended_by"].startswith("session:")


@pytest.mark.asyncio
async def test_a_transition_that_does_not_apply_says_why():
    store = FakeStore(row=_row(status="cancelled"))
    result = json.loads(await _cron_pause_handler({"id": str(store.row.id)}, **_kwargs(FakeSessions(USER), store)))
    assert result["success"] is False and "is cancelled" in result["error"]


@pytest.mark.asyncio
async def test_cron_update_is_confirmed_like_a_new_one():
    store = FakeStore()
    unconfirmed = json.loads(await _cron_update_handler(
        {"id": str(store.row.id), "interval": "2h"}, **_kwargs(FakeSessions(USER, []), store),
    ))
    assert unconfirmed["success"] is False and not any(name == "update" for name, _ in store.calls)
    done = json.loads(await _cron_update_handler(
        {"id": str(store.row.id), "interval": "2h", "prompt": "check the deploy and the logs"},
        **_kwargs(FakeSessions(USER), store),
    ))
    assert done["success"] is True
    update = next(asked for name, asked in store.calls if name == "update")
    assert update["schedule"].seconds == 7200 and update["prompt"] == "check the deploy and the logs"


@pytest.mark.asyncio
async def test_a_scheduled_prompt_cannot_make_another_loop():
    # Every run would make a schedule of its own.
    result = json.loads(await _cron_create_handler(
        {**DIGEST, "prompt": "/loop 5m check the deploy"}, **_kwargs(FakeSessions(USER), FakeStore()),
    ))
    assert result["success"] is False
    assert "/loop" in result["error"]


def test_the_schedule_skill_is_not_gated_on_a_tool():
    # The prompt's tool list is not the one the model is sent, so a
    # requires_tools gate would show it where cron_create is not.
    skills = ResourceLoader()._load_skills_from_dir(str(Path(__file__).parents[1] / "skills"), "platform")
    skill = next(s for s in skills if s.name == "schedule")
    assert not skill.requires_tools
    assert "ask_user_question" in skill.content and "cron_create" in skill.content


def test_loop_list_shows_where_a_schedule_runs_and_when_it_ends():
    lasting = SimpleNamespace(
        id=uuid4(), schedule={}, schedule_display="30 8 * * 1-5", prompt="send the digest", status="active",
        timezone="Europe/Bucharest", next_run_at=datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc), expires_at=None,
    )
    short = SimpleNamespace(
        id=uuid4(), schedule={}, schedule_display="Every 5 minutes", prompt="check the deploy", timezone="UTC",
        status="paused", next_run_at=None,
        expires_at=datetime(2026, 10, 26, 6, 0, 0, 7, tzinfo=timezone.utc),
    )

    assert _format_loop_list([lasting, short]).splitlines() == [
        "Loops:",
        f"- `{lasting.id}` 30 8 * * 1-5: send the digest "
        "(next: 2026-10-26T08:30:00+02:00 Europe/Bucharest; until cancelled)",
        f"- `{short.id}` Every 5 minutes: check the deploy "
        "(paused; next: None; expires 2026-10-26T06:00:00+00:00)",
    ]
