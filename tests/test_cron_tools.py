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
    _cron_list_handler,
)
from surogates.tools.loader import ResourceLoader

ASKED = SimpleNamespace(id=7, type=EventType.ASK_USER_QUESTION_RESPONSE.value, data={})
SAID = SimpleNamespace(id=9, type=EventType.USER_MESSAGE.value, data={"content": "and one more"})


class FakeStore:
    def __init__(self):
        self.created = []
        self.deleted = []
        self.listed = []

    async def create(self, **kwargs):
        row = SimpleNamespace(
            id=uuid4(),
            name=kwargs["name"],
            prompt=kwargs["prompt"],
            schedule_display=kwargs["schedule"].display,
            timezone=kwargs["schedule"].timezone_name,
            next_run_at=datetime(2026, 10, 14, 5, 30, tzinfo=timezone.utc),
            expires_at=kwargs.get("expires_at"),
            status="active",
        )
        self.created.append(kwargs)
        return row

    async def list_for_user(self, **kwargs):
        self.listed.append(kwargs)
        return [
            SimpleNamespace(
                id=uuid4(),
                name="Deploy check",
                prompt="check deploy",
                schedule_display="Every 10 minutes",
                next_run_at=None,
                status="active",
            ),
        ]

    async def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return True


class FakeSessions:
    """The session's sender, and what was said and answered in it."""

    def __init__(self, principal: ActingPrincipal, events: list | None = None):
        self.principal = principal
        self.events = [ASKED] if events is None else events

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


def _kwargs(sessions: FakeSessions, store: FakeStore) -> dict:
    # Managed channels hand the tool the agent's own service account as tenant;
    # the schedule is still the sender's.
    return {
        "tenant": SimpleNamespace(org_id=uuid4(), user_id=None, service_account_id=uuid4()),
        "agent_id": "agent-a",
        "session_id": str(uuid4()),
        "session_store": sessions,
        "scheduled_store": store,
    }


@pytest.mark.asyncio
async def test_cron_create_needs_a_sender_who_owns_it():
    result = json.loads(await _cron_create_handler(
        DIGEST, **_kwargs(FakeSessions(ActingPrincipal(user_id=None, service_account_id=None)), FakeStore()),
    ))
    assert result["success"] is False
    assert "signed-in user or a service account" in result["error"]


@pytest.mark.asyncio
async def test_cron_create_makes_nothing_until_the_user_has_answered_this_turn():
    store = FakeStore()
    for events in ([], [SAID], [ASKED, SAID]):
        result = json.loads(await _cron_create_handler(DIGEST, **_kwargs(FakeSessions(USER, events), store)))
        assert result["success"] is False
        assert "ask_user_question" in result["error"]
        # What it would make, to put to the user.
        assert result["would_create"]["timezone"] == "Europe/Bucharest"
        assert result["would_create"]["next_run_at"].endswith(("+03:00", "+02:00"))
        assert result["would_create"]["runs"] == "until cancelled"
    assert store.created == []


@pytest.mark.asyncio
async def test_cron_create_refuses_a_bad_schedule_before_asking():
    result = json.loads(await _cron_create_handler(
        {**DIGEST, "timezone": "Europe/Bucarest"}, **_kwargs(FakeSessions(USER, []), FakeStore()),
    ))
    assert result == {"success": False, "error": "Unknown timezone: Europe/Bucarest"}


@pytest.mark.parametrize("principal", [USER, SERVICE_ACCOUNT], ids=["user", "service-account"])
@pytest.mark.asyncio
async def test_cron_create_once_confirmed_belongs_to_the_sender(principal):
    store = FakeStore()
    kwargs = _kwargs(FakeSessions(principal), store)
    result = json.loads(await _cron_create_handler(DIGEST, **kwargs))

    assert result["success"] is True
    made = store.created[0]
    assert (made["user_id"], made["service_account_id"]) == (principal.user_id, principal.service_account_id)
    # It lasts: a weekday digest must not stop after a few days.
    assert (made["agent_id"], made["source"], made.get("expires_at")) == ("agent-a", "tool", None)
    assert str(made["created_from_session_id"]) == kwargs["session_id"]
    assert made["prompt"] == DIGEST["prompt"]
    # Its next run on its own clock, to restate in any language.
    assert result["schedule"]["next_run_at"] == "2026-10-14T08:30:00+03:00"
    assert result["schedule"]["timezone"] == "Europe/Bucharest"


@pytest.mark.asyncio
async def test_cron_create_once_is_not_recurring():
    store = FakeStore()
    result = json.loads(await _cron_create_handler(
        {**DIGEST, "recurring": False}, **_kwargs(FakeSessions(USER, []), store),
    ))
    assert result["would_create"]["runs"] == "once"
    await _cron_create_handler({**DIGEST, "recurring": False}, **_kwargs(FakeSessions(USER), store))
    assert store.created[0]["repeat_limit"] == 1


@pytest.mark.parametrize("principal", [USER, SERVICE_ACCOUNT], ids=["user", "service-account"])
@pytest.mark.asyncio
async def test_cron_list_and_delete_are_the_senders(principal):
    store = FakeStore()
    listed = json.loads(await _cron_list_handler({}, **_kwargs(FakeSessions(principal), store)))
    schedule_id = uuid4()
    deleted = json.loads(await _cron_delete_handler({"id": str(schedule_id)}, **_kwargs(FakeSessions(principal), store)))

    assert listed["success"] is True
    assert listed["schedules"][0]["name"] == "Deploy check"
    assert deleted["success"] is True
    for asked in (store.listed[0], store.deleted[0]):
        assert (asked["user_id"], asked["service_account_id"]) == (principal.user_id, principal.service_account_id)
    assert store.deleted[0]["schedule_id"] == schedule_id


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
        id=uuid4(), schedule={}, schedule_display="30 8 * * 1-5", prompt="send the digest",
        timezone="Europe/Bucharest", next_run_at=datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc), expires_at=None,
    )
    short = SimpleNamespace(
        id=uuid4(), schedule={}, schedule_display="Every 5 minutes", prompt="check the deploy", timezone="UTC",
        next_run_at=datetime(2026, 10, 23, 6, 5, tzinfo=timezone.utc),
        expires_at=datetime(2026, 10, 26, 6, 0, 0, 7, tzinfo=timezone.utc),
    )

    assert _format_loop_list([lasting, short]).splitlines() == [
        "Active loops:",
        f"- `{lasting.id}` 30 8 * * 1-5: send the digest "
        "(next: 2026-10-26T08:30:00+02:00 Europe/Bucharest; until cancelled)",
        f"- `{short.id}` Every 5 minutes: check the deploy "
        "(next: 2026-10-23T06:05:00+00:00 UTC; expires 2026-10-26T06:00:00+00:00)",
    ]
