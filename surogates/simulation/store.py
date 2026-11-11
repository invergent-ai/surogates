"""Runs and their worlds, in the runtime database.

A run's credentials live in the vault under the names the runtime's adapter
already reads (``whatsapp_<cred>_<phone_number_id>``,
``telegram_<cred>_<bot username>``), so the real adapter serves a simulation
account exactly as it serves a real one.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

import sqlalchemy as sa

from surogates.channels.identity import ResolvedIdentity, get_or_create_channel_identity
from surogates.channels.platforms.whatsapp_api import DEFAULT_API_VERSION
from surogates.db.models import (
    Event,
    FollowupEnrollmentRow,
    Mission,
    OutreachMessageRow,
    ProgramInvitationRow,
    ProgramOccurrenceRow,
    ScheduledSession,
    Session,
    SessionLease,
    SimulationMedia,
    SimulationMessage,
    SimulationPerson,
    SimulationRun,
    User,
)
from surogates.harness.landing import TURN_ENDS
from surogates.simulation.ids import (
    is_simulation_id,
    new_bot_token,
    new_bot_username,
    new_discord_bot_token,
    new_discord_id,
    new_phone_number_id,
    new_slack_app_id,
    new_slack_bot_token,
    new_slack_user_id,
    new_teams_app_id,
    new_teams_user_id,
    new_telegram_user_id,
    new_voice_number,
    new_wa_id,
    new_website_key,
)

logger = logging.getLogger(__name__)

#: A run still running this long after it started was left open: nobody plays it.
STALE_AFTER = timedelta(hours=2)
KEEP_FOR = timedelta(days=90)
#: How long a run whose people are all done must stay quiet before it ends.
GRACE = timedelta(seconds=60)
#: How a person's part can end; a run whose people all ended is over.
_ENDED = ("done", "gave_up", "no_reply", "failed")

VAULT_CREDENTIALS = {
    "whatsapp": ("access_token", "app_secret", "verify_token", "phone_number_id", "api_version"),
    "telegram": ("bot_token", "webhook_secret"),
    "slack": ("bot_token", "signing_secret"),
    "teams": ("app_password", "tenant_id"),
    "discord": ("bot_token",),
    # The widget talks to the runtime API itself: nothing to keep in the vault.
    "website": (),
    # A call reaches the voice worker through LiveKit, which the runtime holds the keys to.
    "voice": (),
}
CHANNELS = tuple(VAULT_CREDENTIALS)

_WINDOW = timedelta(hours=24)
#: How long each platform shows "typing…" unless the bot refreshes it or sends
#: a message: WhatsApp 25 s, Telegram 5 s, Teams about 3 s (given 5 here, as
#: the adapter refreshes every 3 s and Studio polls in between).
_TYPING = {"whatsapp": timedelta(seconds=25), "telegram": timedelta(seconds=5),
           "teams": timedelta(seconds=5), "discord": timedelta(seconds=10)}

Provision = Callable[..., Awaitable[ResolvedIdentity]]


async def create_run(
    sf, vault, *, org_id: uuid.UUID, agent_id: str, created_by: str,
    routing_config: dict[str, Any], people: list[dict[str, Any]],
    provision: Provision = get_or_create_channel_identity,
    templates: list[dict[str, Any]] | None = None,
    flows: list[dict[str, Any]] | None = None,
    channel: str = "whatsapp",
    batch_id: uuid.UUID | None = None,
    via: str | None = None,
) -> SimulationRun:
    """A run with its plan's *people* (``{name, about, test, member_id?, autopilot?}``):
    customers, and a stand-in for each team member a line names."""
    if channel == "telegram":
        account, token = new_bot_username(), new_bot_token()
    elif channel == "slack":
        account, token = new_slack_app_id(), new_slack_bot_token()
    elif channel == "teams":
        account, token = new_teams_app_id(), "eyJ" + secrets.token_urlsafe(48)
    elif channel == "website":
        account, token = new_website_key(), secrets.token_hex(16)
    elif channel == "voice":
        account, token = new_voice_number(), secrets.token_hex(16)
    elif channel == "discord":
        account = new_discord_id()
        token = new_discord_bot_token(account)
    else:
        account, token = new_phone_number_id(), "EAA" + secrets.token_urlsafe(96)
    run = SimulationRun(
        org_id=org_id, agent_id=agent_id, created_by=created_by, channel=channel,
        phone_number_id=account, access_token=token, app_secret=secrets.token_hex(16),
        routing_config=dict(routing_config or {}), templates=list(templates or []),
        flows=list(flows or []), via=via, batch_id=batch_id,
    )
    async with sf() as db:
        db.add(run)
        await db.commit()
    if channel == "telegram":
        creds = {"bot_token": run.access_token, "webhook_secret": run.app_secret}
    elif channel == "slack":
        creds = {"bot_token": run.access_token, "signing_secret": run.app_secret}
    elif channel == "teams":
        # The world's tenant is the App ID: one bot, one tenant.
        creds = {"app_password": run.app_secret, "tenant_id": run.phone_number_id}
    elif channel == "discord":
        creds = {"bot_token": run.access_token}
    elif channel in ("website", "voice"):
        creds = {}
    else:
        creds = {
            "access_token": run.access_token, "app_secret": run.app_secret,
            "verify_token": secrets.token_hex(8), "phone_number_id": run.phone_number_id,
            "api_version": DEFAULT_API_VERSION,
        }
    for name, value in creds.items():
        await vault.store(org_id, f"{channel}_{name}_{run.phone_number_id}", value)
    for line in people:
        if not line.get("member_id"):
            await add_person(sf, run, name=line["name"], provision=provision, about=line.get("about"),
                             test=line.get("test"), autopilot=bool(line.get("autopilot")))
    from surogates.simulation.team import add_team

    await add_team(sf, run, provision, members=[line for line in people if line.get("member_id")])
    return run


def customer_persona(name: str, about: str | None, test: str | None) -> str:
    """Who Autopilot plays for a customer line."""
    return " ".join(part for part in (f"You are {name}.", about,
                                      f"Stay in character; your aim in this chat: {test or about or ''}")
                    if part)


async def add_person(sf, run: SimulationRun, *, name: str, provision: Provision,
                     about: str | None = None, test: str | None = None,
                     autopilot: bool = False) -> SimulationPerson:
    if run.channel == "website":
        # A visitor is anonymous: the widget's own cookie names them, no user row.
        person = SimulationPerson(run_id=run.id, name=name, channel=run.channel, address="visitor",
                                  about=about, test=test, persona=customer_persona(name, about, test),
                                  driver="ai" if autopilot else "human",
                                  ai_state="talking" if autopilot else None)
        async with sf() as db:
            db.add(person)
            await db.commit()
        return person
    address = new_address(run.channel)
    # The same call the inbound pipeline makes for an unknown sender: the
    # pipeline then finds this identity, already marked as the run's.
    identity = await provision(
        sf, platform=run.channel, platform_user_id=address, org_id=run.org_id, display_name=name,
    )
    person = SimulationPerson(run_id=run.id, name=name, channel=run.channel, address=address,
                              user_id=identity.user_id, about=about, test=test,
                              persona=customer_persona(name, about, test),
                              driver="ai" if autopilot else "human",
                              ai_state="talking" if autopilot else None)
    async with sf() as db:
        await db.execute(sa.update(User).where(User.id == identity.user_id)
                         .values(simulation_run_id=run.id))
        db.add(person)
        await db.commit()
    return person


def new_address(channel: str) -> str:
    """A fresh person's address on *channel*."""
    return {"telegram": new_telegram_user_id, "slack": new_slack_user_id,
            "teams": new_teams_user_id, "discord": new_discord_id,
            "voice": new_voice_number}.get(channel, new_wa_id)()


async def ai_people(sf) -> Sequence[tuple[SimulationRun, SimulationPerson]]:
    """The people the AI is writing as right now, with their runs."""
    async with sf() as db:
        rows = await db.execute(
            sa.select(SimulationRun, SimulationPerson)
            .join(SimulationPerson, SimulationPerson.run_id == SimulationRun.id)
            .where(SimulationRun.status == "running", SimulationPerson.driver == "ai",
                   SimulationPerson.ai_state == "talking")
        )
        return [tuple(r) for r in rows.all()]


async def visitors(sf) -> Sequence[tuple[SimulationRun, SimulationPerson]]:
    """The website people of running runs who have written, with their runs."""
    async with sf() as db:
        rows = await db.execute(
            sa.select(SimulationRun, SimulationPerson)
            .join(SimulationPerson, SimulationPerson.run_id == SimulationRun.id)
            .where(SimulationRun.status == "running", SimulationRun.channel == "website",
                   SimulationPerson.address != "visitor")
        )
        return [tuple(r) for r in rows.all()]


async def update_person(sf, person_id: uuid.UUID, **values: Any) -> None:
    async with sf() as db:
        await db.execute(sa.update(SimulationPerson).where(SimulationPerson.id == person_id).values(**values))
        await db.commit()


async def set_review(sf, run_id: uuid.UUID, review: dict[str, Any]) -> None:
    async with sf() as db:
        await db.execute(sa.update(SimulationRun).where(SimulationRun.id == run_id).values(review=review))
        await db.commit()


async def get_run(sf, run_id: uuid.UUID) -> SimulationRun | None:
    async with sf() as db:
        return await db.get(SimulationRun, run_id)


async def run_by_number(sf, phone_number_id: str) -> SimulationRun | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationRun).where(SimulationRun.phone_number_id == phone_number_id)
        )).scalar_one_or_none()


async def run_by_token(sf, token: str) -> SimulationRun | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationRun).where(SimulationRun.access_token == token)
        )).scalar_one_or_none()


async def list_runs(sf, *, org_id: uuid.UUID, agent_id: str, limit: int = 50) -> Sequence[SimulationRun]:
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationRun)
            .where(SimulationRun.org_id == org_id, SimulationRun.agent_id == agent_id)
            .order_by(SimulationRun.created_at.desc())
            .limit(limit)
        )).scalars().all()


async def batch_runs(sf, *, org_id: uuid.UUID, agent_id: str,
                     batch_id: uuid.UUID) -> Sequence[SimulationRun]:
    """The runs of one plan, oldest first; a run started on its own is its own batch."""
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationRun)
            .where(SimulationRun.org_id == org_id, SimulationRun.agent_id == agent_id)
            .where((SimulationRun.batch_id == batch_id)
                   | ((SimulationRun.id == batch_id) & SimulationRun.batch_id.is_(None)))
            .order_by(SimulationRun.created_at)
        )).scalars().all()


async def people_of(sf, run_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[SimulationPerson]]:
    """Each run's people, in the order they were added."""
    found: dict[uuid.UUID, list[SimulationPerson]] = {run_id: [] for run_id in run_ids}
    if not run_ids:
        return found
    async with sf() as db:
        for person in (await db.execute(
            sa.select(SimulationPerson).where(SimulationPerson.run_id.in_(run_ids))
            .order_by(SimulationPerson.created_at)
        )).scalars():
            found[person.run_id].append(person)
    return found


async def enrolled(sf, run: SimulationRun, user_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Which of *user_ids* the run's agent follows in a program right now."""
    if not user_ids:
        return set()
    async with sf() as db:
        return set((await db.scalars(
            sa.select(FollowupEnrollmentRow.user_id)
            .where(FollowupEnrollmentRow.org_id == run.org_id,
                   FollowupEnrollmentRow.agent_id == run.agent_id,
                   FollowupEnrollmentRow.status == "active",
                   FollowupEnrollmentRow.user_id.in_(user_ids))
        )).all())


async def message_counts(sf, run_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not run_ids:
        return {}
    async with sf() as db:
        rows = await db.execute(
            sa.select(SimulationMessage.run_id, sa.func.count())
            .where(SimulationMessage.run_id.in_(run_ids))
            .group_by(SimulationMessage.run_id)
        )
        return {run_id: count for run_id, count in rows.all()}


async def team_messages(sf, user_ids: Sequence[uuid.UUID]) -> list[dict[str, Any]]:
    """What the agent told its team about these people, oldest first."""
    if not user_ids:
        return []
    async with sf() as db:
        rows = (await db.execute(
            sa.select(OutreachMessageRow)
            .where(OutreachMessageRow.subject_user_id.in_(user_ids))
            .order_by(OutreachMessageRow.created_at)
        )).scalars().all()
        names = dict((await db.execute(
            sa.select(User.id, User.display_name)
            .where(User.id.in_({row.user_id for row in rows}))
        )).all()) if rows else {}
    return [
        {"id": str(row.id), "at": row.created_at.isoformat(), "kind": row.kind,
         "platform": row.platform, "to": row.destination or names.get(row.user_id) or "The team",
         "group": row.destination is not None, "text": row.text, "severity": row.severity}
        for row in rows
    ]


async def people(sf, run_id: uuid.UUID) -> Sequence[SimulationPerson]:
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationPerson).where(SimulationPerson.run_id == run_id)
            .order_by(SimulationPerson.created_at)
        )).scalars().all()


async def person_by_address(sf, run_id: uuid.UUID, address: str) -> SimulationPerson | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationPerson).where(
                SimulationPerson.run_id == run_id, SimulationPerson.address == address,
            )
        )).scalar_one_or_none()


async def log_message(
    sf, *, run_id: uuid.UUID, person_id: uuid.UUID, direction: str, kind: str,
    body: dict[str, Any], wamid: str, status: str = "",
) -> SimulationMessage:
    message = SimulationMessage(run_id=run_id, person_id=person_id, direction=direction, kind=kind,
                               body=body, wamid=wamid, status=status)
    async with sf() as db:
        db.add(message)
        if direction == "out":
            # A team member is never done for good: the agent writing again wakes them.
            await db.execute(sa.update(SimulationPerson)
                             .where(SimulationPerson.id == person_id,
                                    SimulationPerson.stands_for.isnot(None),
                                    SimulationPerson.driver == "ai",
                                    SimulationPerson.ai_state == "done")
                             .values(ai_state="talking", ai_note=None))
        await db.commit()
    return message


async def replace_body(sf, run_id: uuid.UUID, wamid: str, body: dict[str, Any]) -> bool:
    """An edit: the message keeps its place in the thread with its new body.

    Its time becomes the edit's: an agent that edits a placeholder ("Thinking…")
    into its answer has just spoken, and the person's turn follows that.
    """
    async with sf() as db:
        result = await db.execute(sa.update(SimulationMessage)
                                  .where(SimulationMessage.run_id == run_id,
                                         SimulationMessage.wamid == wamid)
                                  .values(body=body, created_at=datetime.now(timezone.utc)))
        await db.commit()
    return result.rowcount > 0


async def delete_message(sf, run_id: uuid.UUID, wamid: str) -> bool:
    async with sf() as db:
        result = await db.execute(sa.delete(SimulationMessage).where(
            SimulationMessage.run_id == run_id, SimulationMessage.wamid == wamid,
            SimulationMessage.direction == "out",
        ))
        await db.commit()
    return result.rowcount > 0


async def message(sf, run_id: uuid.UUID, wamid: str) -> SimulationMessage | None:
    async with sf() as db:
        return (await db.execute(sa.select(SimulationMessage).where(
            SimulationMessage.run_id == run_id, SimulationMessage.wamid == wamid,
        ))).scalar_one_or_none()


async def next_message_id(sf, run_id: uuid.UUID) -> int:
    """Telegram numbers a chat's messages 1, 2, 3…; a run has one chat per person."""
    async with sf() as db:
        count = (await db.execute(sa.select(sa.func.count()).select_from(SimulationMessage)
                                  .where(SimulationMessage.run_id == run_id))).scalar_one()
    return int(count) + 1


async def set_status(sf, run_id: uuid.UUID, wamid: str, status: str) -> None:
    async with sf() as db:
        await db.execute(sa.update(SimulationMessage)
                         .where(SimulationMessage.run_id == run_id, SimulationMessage.wamid == wamid)
                         .values(status=status))
        await db.commit()


async def mark_read_up_to(sf, run_id: uuid.UUID, wamid: str) -> None:
    """A read receipt turns that message, and every earlier one from the person, blue."""
    async with sf() as db:
        target = (await db.execute(sa.select(SimulationMessage).where(
            SimulationMessage.run_id == run_id, SimulationMessage.wamid == wamid,
            SimulationMessage.direction == "in",
        ))).scalar_one_or_none()
        if target is None:
            return
        await db.execute(sa.update(SimulationMessage).where(
            SimulationMessage.person_id == target.person_id,
            SimulationMessage.direction == "in",
            SimulationMessage.id <= target.id,
        ).values(status="read"))
        await db.commit()


async def set_typing(sf, run_id: uuid.UUID, wamid: str, typing: bool) -> None:
    """Show or clear "typing…" for the person the message *wamid* belongs to."""
    async with sf() as db:
        person_id = (await db.execute(sa.select(SimulationMessage.person_id).where(
            SimulationMessage.run_id == run_id, SimulationMessage.wamid == wamid,
        ))).scalar_one_or_none()
        if person_id is not None:
            await db.execute(sa.update(SimulationPerson).where(SimulationPerson.id == person_id)
                             .values(agent_typing_at=datetime.now(timezone.utc) if typing else None))
            await db.commit()


async def set_person_typing(sf, person_id: uuid.UUID, typing: bool) -> None:
    async with sf() as db:
        await db.execute(sa.update(SimulationPerson).where(SimulationPerson.id == person_id)
                         .values(agent_typing_at=datetime.now(timezone.utc) if typing else None))
        await db.commit()


def is_typing(person: SimulationPerson, now: datetime) -> bool:
    shown = _TYPING.get(person.channel, timedelta(seconds=5))
    return person.agent_typing_at is not None and now - person.agent_typing_at < shown


def _theirs(person: SimulationPerson):
    """*person*'s conversations: their user's sessions, or a visitor's one widget session."""
    if person.user_id is not None:
        return Session.user_id == person.user_id
    from surogates.simulation.website import visitor_session

    session = visitor_session(person)
    # Not sa.false(): that would drop sessions from the queries' FROM.
    return Session.id == uuid.UUID(session[0]) if session else Session.id.is_(None)


async def agent_working(sf, person: SimulationPerson) -> bool:
    """Whether the agent owes *person* an answer: a turn is running in one of
    their conversations, or their last message has not been taken up yet."""
    def latest(*types: str, said: bool = False):
        query = (sa.select(sa.func.coalesce(sa.func.max(Event.id), 0))
                 .where(Event.session_id == Session.id, Event.type.in_(types)))
        if said:  # An outreach opener is logged as a message too, but nobody said it.
            query = query.where(Event.data["synthetic"].as_string().is_(None))
        return query.scalar_subquery()

    async with sf() as db:
        return bool(await db.scalar(sa.select(
            sa.exists().where(
                SessionLease.session_id == Session.id, _theirs(person),
                SessionLease.expires_at > sa.func.now(),
                # A question waiting on its answer holds the lease too, but
                # then the person is the one owed a move.
                latest("inbox.input_required") <= latest("user.message", "ask_user_question.response"),
            )
            | sa.exists().where(
                _theirs(person),
                latest("user.message", said=True) > latest(*TURN_ENDS, "inbox.input_required"),
            )
        )))


async def agent_turn_ended_at(sf, person: SimulationPerson) -> datetime | None:
    """When the agent last finished a turn in one of *person*'s conversations."""
    async with sf() as db:
        ended = await db.scalar(
            sa.select(sa.func.max(Event.created_at))
            .join(Session, Session.id == Event.session_id)
            # A sub-agent finishing is not the agent's turn ending.
            .where(_theirs(person), Session.parent_id.is_(None),
                   Event.type == "session.complete")
        )
    if ended is not None and ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return ended


async def left_unanswered(sf, person: SimulationPerson) -> bool:
    """Whether the agent's turn on *person*'s last message completed without a
    word to them: it chose to stay silent (a thank-you needs no answer)."""
    def latest(*types: str):
        return (sa.select(sa.func.coalesce(sa.func.max(Event.id), 0))
                .where(Event.session_id == Session.id, Event.type.in_(types))
                .scalar_subquery())

    async with sf() as db:
        return bool(await db.scalar(sa.select(sa.exists().where(
            _theirs(person),
            latest("session.complete") > latest("user.message"),
            latest("session.fail", "session.stopped") < latest("user.message"),
        ))))


async def thread(sf, person_id: uuid.UUID) -> Sequence[SimulationMessage]:
    async with sf() as db:
        return (await db.execute(
            sa.select(SimulationMessage).where(SimulationMessage.person_id == person_id)
            .order_by(SimulationMessage.id)
        )).scalars().all()


def with_template_definition(run: SimulationRun, body: dict[str, Any]) -> dict[str, Any]:
    """A template send, with the template's components as the account defines them."""
    sent = body.get("template") or {}
    language = (sent.get("language") or {}).get("code")
    for template in run.templates or []:
        if template.get("name") == sent.get("name") and template.get("language") in (language, None):
            return {**body, "template": {**sent, "definition": template.get("components") or []}}
    return body


def window_open(messages: Sequence[SimulationMessage], now: datetime) -> bool:
    """WhatsApp's customer-service window: 24 hours from the person's last message."""
    last_in = max((m.created_at for m in messages if m.direction == "in"), default=None)
    return last_in is not None and now - last_in < _WINDOW


async def put_media(sf, run_id: uuid.UUID, data: bytes, mime_type: str) -> str:
    media_id = f"media{secrets.token_hex(8)}"
    async with sf() as db:
        db.add(SimulationMedia(id=media_id, run_id=run_id, mime_type=mime_type, data=data))
        await db.commit()
    return media_id


async def get_media(sf, run_id: uuid.UUID, media_id: str) -> SimulationMedia | None:
    async with sf() as db:
        media = await db.get(SimulationMedia, media_id)
    return media if media is not None and media.run_id == run_id else None


async def routing_for(sf, kind: str, identifier: str) -> dict[str, Any] | None:
    """The routing record a running simulation number resolves to, else ``None``."""
    if kind not in CHANNELS or not is_simulation_id(identifier):
        return None
    run = await run_by_number(sf, identifier)
    if run is None or run.status != "running" or run.channel != kind:
        return None
    return {
        "org_id": str(run.org_id), "agent_id": run.agent_id,
        "config": {**(run.routing_config or {}), "simulation_run_id": str(run.id)},
    }


async def running_routings(sf, kind: str) -> list[dict[str, Any]]:
    """Every running simulation on *kind*, shaped as ops lists a channel's routings."""
    async with sf() as db:
        runs = (await db.execute(sa.select(SimulationRun).where(
            SimulationRun.channel == kind, SimulationRun.status == "running",
        ))).scalars().all()
    return [{"channel_identifier": r.phone_number_id, "org_id": str(r.org_id),
             "config": dict(r.routing_config or {})} for r in runs]


async def end_run(sf, vault, run_id: uuid.UUID) -> SimulationRun | None:
    async with sf() as db:
        run = await db.get(SimulationRun, run_id)
        if run is None:
            return None
        run.status, run.ended_at = "ended", datetime.now(timezone.utc)
        await db.commit()
    for name in VAULT_CREDENTIALS[run.channel]:
        await vault.delete(run.org_id, f"{run.channel}_{name}_{run.phone_number_id}")
    return run


async def finish_run(state, run_id: uuid.UUID) -> SimulationRun | None:
    """End the run and take its world down: credentials, Discord gateway, calls."""
    from surogates.simulation import discord, voice

    run = await end_run(state.session_factory, state.vault, run_id)
    if run is None:
        return None
    if run.channel == "discord":
        await discord.GATEWAYS.close(run.id)
    if run.channel == "voice" and state.settings.voice.livekit_api_key:
        try:
            await voice.hang_up_all(state.settings, run)
        except Exception:  # noqa: BLE001 - the run is ended; a call left ringing ends at its own limit
            logger.warning("could not hang up the calls of simulation %s", run.id, exc_info=True)
    return run


async def done_runs(sf, now: datetime) -> Sequence[uuid.UUID]:
    """Running runs the AI plays alone whose every person's part has ended, and
    quiet for GRACE: the agent may still be relaying to its team, which wakes a
    stand-in that was done."""
    async with sf() as db:
        return (await db.execute(sa.select(SimulationRun.id).where(
            SimulationRun.status == "running",
            # A run is committed before its people are added.
            sa.exists().where(SimulationPerson.run_id == SimulationRun.id),
            ~sa.exists().where(
                SimulationPerson.run_id == SimulationRun.id,
                (SimulationPerson.driver != "ai")
                | sa.func.coalesce(SimulationPerson.ai_state, "").notin_(_ENDED),
            ),
            ~sa.exists().where(SimulationMessage.run_id == SimulationRun.id,
                               SimulationMessage.created_at > now - GRACE),
        ))).scalars().all()


async def delete_run(sf, run_id: uuid.UUID) -> None:
    """A run with its people, messages, media, stand-in users and their conversations."""
    from surogates.session.store import delete_missions, delete_session_rows

    async with sf() as db:
        users = (await db.execute(
            sa.select(User.id).where(User.simulation_run_id == run_id))).scalars().all()
        tree = set((await db.execute(sa.select(Session.id).where(
            Session.user_id.in_(users)
            | (Session.config["simulation_run_id"].as_string() == str(run_id))
        ))).scalars().all())
        while True:  # sub-agents' sessions, at any depth
            children = set((await db.execute(sa.select(Session.id).where(
                Session.parent_id.in_(tree)))).scalars().all()) - tree
            if not children:
                break
            tree |= children
        missions = (await db.execute(sa.select(Mission.id).where(
            Mission.session_id.in_(tree) | Mission.user_id.in_(users)))).scalars().all()
        if missions:
            await delete_missions(db, missions)
        await delete_session_rows(db, list(tree))
        await db.execute(sa.delete(ScheduledSession).where(ScheduledSession.user_id.in_(users)))
        await db.execute(sa.delete(OutreachMessageRow).where(
            OutreachMessageRow.user_id.in_(users) | OutreachMessageRow.subject_user_id.in_(users)))
        # Program rows name people by id with no foreign key: nothing else clears them.
        theirs = (ProgramInvitationRow.user_id.in_(users)
                  | ProgramInvitationRow.person_user_id.in_(users))
        occurrences = sa.select(ProgramInvitationRow.occurrence_id).where(theirs).scalar_subquery()
        await db.execute(sa.delete(ProgramOccurrenceRow).where(
            ProgramOccurrenceRow.id.in_(occurrences), ProgramOccurrenceRow.status == "simulated"))
        await db.execute(sa.delete(ProgramInvitationRow).where(theirs))
        await db.execute(sa.delete(FollowupEnrollmentRow).where(FollowupEnrollmentRow.user_id.in_(users)))
        # Whatever else names a stand-in: emptied where it may be, deleted where
        # it must name someone (channel identities, devices...).
        for table in User.metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column is not User.__table__.c.id or fk.ondelete == "CASCADE":
                    continue
                where = fk.parent.in_(users)
                if fk.parent.nullable:
                    await db.execute(sa.update(table).where(where).values({fk.parent.name: None}))
                else:
                    await db.execute(sa.delete(table).where(where))
        await db.execute(sa.delete(User).where(User.id.in_(users)))
        # People, messages and media go with the run (ON DELETE CASCADE).
        await db.execute(sa.delete(SimulationRun).where(SimulationRun.id == run_id))
        await db.commit()


async def sweep(state, now: datetime) -> None:
    """End runs left running, and delete ended runs older than KEEP_FOR."""
    sf = state.session_factory
    async with sf() as db:
        stale = (await db.execute(sa.select(SimulationRun.id).where(
            SimulationRun.status == "running", SimulationRun.created_at < now - STALE_AFTER,
        ))).scalars().all()
        old = (await db.execute(sa.select(SimulationRun.id).where(
            SimulationRun.status == "ended", SimulationRun.created_at < now - KEEP_FOR,
        ))).scalars().all()
    for run_id in stale:
        try:
            await finish_run(state, run_id)
        except Exception:
            logger.exception("[simulation] could not end stale run %s", run_id)
    for run_id in old:
        try:
            await delete_run(sf, run_id)
        except Exception:
            logger.exception("[simulation] could not delete old run %s", run_id)
