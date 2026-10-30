"""Runs and their worlds, in the runtime database.

A run's credentials live in the vault under the names the runtime's adapter
already reads (``whatsapp_<cred>_<phone_number_id>``,
``telegram_<cred>_<bot username>``), so the real adapter serves a rehearsal
account exactly as it serves a real one.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence

import sqlalchemy as sa

from surogates.channels.identity import ResolvedIdentity, get_or_create_channel_identity
from surogates.channels.platforms.whatsapp_api import DEFAULT_API_VERSION
from surogates.db.models import RehearsalMedia, RehearsalMessage, RehearsalPerson, RehearsalRun, User
from surogates.rehearsal.ids import (
    is_rehearsal_id,
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
    routing_config: dict[str, Any], person_name: str,
    provision: Provision = get_or_create_channel_identity,
    templates: list[dict[str, Any]] | None = None,
    flows: list[dict[str, Any]] | None = None,
    channel: str = "whatsapp",
    persona: str | None = None,
) -> RehearsalRun:
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
    run = RehearsalRun(
        org_id=org_id, agent_id=agent_id, created_by=created_by, channel=channel,
        phone_number_id=account, access_token=token, app_secret=secrets.token_hex(16),
        routing_config=dict(routing_config or {}), templates=list(templates or []),
        flows=list(flows or []),
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
    await add_person(sf, run, name=person_name, provision=provision, persona=persona)
    return run


async def add_person(sf, run: RehearsalRun, *, name: str, provision: Provision,
                     persona: str | None = None) -> RehearsalPerson:
    if run.channel == "website":
        # A visitor is anonymous: the widget's own cookie names them, no user row.
        person = RehearsalPerson(run_id=run.id, name=name, channel=run.channel, address="visitor")
        async with sf() as db:
            db.add(person)
            await db.commit()
        return person
    address = {"telegram": new_telegram_user_id, "slack": new_slack_user_id,
               "teams": new_teams_user_id, "discord": new_discord_id,
               "voice": new_voice_number}.get(
        run.channel, new_wa_id)()
    # The same call the inbound pipeline makes for an unknown sender: the
    # pipeline then finds this identity, already marked as the run's.
    identity = await provision(
        sf, platform=run.channel, platform_user_id=address, org_id=run.org_id, display_name=name,
    )
    person = RehearsalPerson(run_id=run.id, name=name, channel=run.channel, address=address,
                             user_id=identity.user_id, persona=persona,
                             driver="ai" if persona else "human",
                             ai_state="talking" if persona else None)
    async with sf() as db:
        await db.execute(sa.update(User).where(User.id == identity.user_id)
                         .values(rehearsal_run_id=run.id))
        db.add(person)
        await db.commit()
    return person


async def ai_people(sf) -> Sequence[tuple[RehearsalRun, RehearsalPerson]]:
    """The people the AI is writing as right now, with their runs."""
    async with sf() as db:
        rows = await db.execute(
            sa.select(RehearsalRun, RehearsalPerson)
            .join(RehearsalPerson, RehearsalPerson.run_id == RehearsalRun.id)
            .where(RehearsalRun.status == "running", RehearsalPerson.driver == "ai",
                   RehearsalPerson.ai_state == "talking")
        )
        return [tuple(r) for r in rows.all()]


async def update_person(sf, person_id: uuid.UUID, **values: Any) -> None:
    async with sf() as db:
        await db.execute(sa.update(RehearsalPerson).where(RehearsalPerson.id == person_id).values(**values))
        await db.commit()


async def get_run(sf, run_id: uuid.UUID) -> RehearsalRun | None:
    async with sf() as db:
        return await db.get(RehearsalRun, run_id)


async def run_by_number(sf, phone_number_id: str) -> RehearsalRun | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalRun).where(RehearsalRun.phone_number_id == phone_number_id)
        )).scalar_one_or_none()


async def run_by_token(sf, token: str) -> RehearsalRun | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalRun).where(RehearsalRun.access_token == token)
        )).scalar_one_or_none()


async def list_runs(sf, *, org_id: uuid.UUID, agent_id: str) -> Sequence[RehearsalRun]:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalRun)
            .where(RehearsalRun.org_id == org_id, RehearsalRun.agent_id == agent_id)
            .order_by(RehearsalRun.created_at.desc())
            .limit(50)
        )).scalars().all()


async def people(sf, run_id: uuid.UUID) -> Sequence[RehearsalPerson]:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalPerson).where(RehearsalPerson.run_id == run_id)
            .order_by(RehearsalPerson.created_at)
        )).scalars().all()


async def person_by_address(sf, run_id: uuid.UUID, address: str) -> RehearsalPerson | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalPerson).where(
                RehearsalPerson.run_id == run_id, RehearsalPerson.address == address,
            )
        )).scalar_one_or_none()


async def log_message(
    sf, *, run_id: uuid.UUID, person_id: uuid.UUID, direction: str, kind: str,
    body: dict[str, Any], wamid: str, status: str = "",
) -> RehearsalMessage:
    message = RehearsalMessage(run_id=run_id, person_id=person_id, direction=direction, kind=kind,
                               body=body, wamid=wamid, status=status)
    async with sf() as db:
        db.add(message)
        await db.commit()
    return message


async def replace_body(sf, run_id: uuid.UUID, wamid: str, body: dict[str, Any]) -> bool:
    """An edit: the message keeps its place in the thread with its new body."""
    async with sf() as db:
        result = await db.execute(sa.update(RehearsalMessage)
                                  .where(RehearsalMessage.run_id == run_id,
                                         RehearsalMessage.wamid == wamid)
                                  .values(body=body))
        await db.commit()
    return result.rowcount > 0


async def delete_message(sf, run_id: uuid.UUID, wamid: str) -> bool:
    async with sf() as db:
        result = await db.execute(sa.delete(RehearsalMessage).where(
            RehearsalMessage.run_id == run_id, RehearsalMessage.wamid == wamid,
            RehearsalMessage.direction == "out",
        ))
        await db.commit()
    return result.rowcount > 0


async def message(sf, run_id: uuid.UUID, wamid: str) -> RehearsalMessage | None:
    async with sf() as db:
        return (await db.execute(sa.select(RehearsalMessage).where(
            RehearsalMessage.run_id == run_id, RehearsalMessage.wamid == wamid,
        ))).scalar_one_or_none()


async def next_message_id(sf, run_id: uuid.UUID) -> int:
    """Telegram numbers a chat's messages 1, 2, 3…; a run has one chat per person."""
    async with sf() as db:
        count = (await db.execute(sa.select(sa.func.count()).select_from(RehearsalMessage)
                                  .where(RehearsalMessage.run_id == run_id))).scalar_one()
    return int(count) + 1


async def set_status(sf, run_id: uuid.UUID, wamid: str, status: str) -> None:
    async with sf() as db:
        await db.execute(sa.update(RehearsalMessage)
                         .where(RehearsalMessage.run_id == run_id, RehearsalMessage.wamid == wamid)
                         .values(status=status))
        await db.commit()


async def mark_read_up_to(sf, run_id: uuid.UUID, wamid: str) -> None:
    """A read receipt turns that message, and every earlier one from the person, blue."""
    async with sf() as db:
        target = (await db.execute(sa.select(RehearsalMessage).where(
            RehearsalMessage.run_id == run_id, RehearsalMessage.wamid == wamid,
            RehearsalMessage.direction == "in",
        ))).scalar_one_or_none()
        if target is None:
            return
        await db.execute(sa.update(RehearsalMessage).where(
            RehearsalMessage.person_id == target.person_id,
            RehearsalMessage.direction == "in",
            RehearsalMessage.id <= target.id,
        ).values(status="read"))
        await db.commit()


async def set_typing(sf, run_id: uuid.UUID, wamid: str, typing: bool) -> None:
    """Show or clear "typing…" for the person the message *wamid* belongs to."""
    async with sf() as db:
        person_id = (await db.execute(sa.select(RehearsalMessage.person_id).where(
            RehearsalMessage.run_id == run_id, RehearsalMessage.wamid == wamid,
        ))).scalar_one_or_none()
        if person_id is not None:
            await db.execute(sa.update(RehearsalPerson).where(RehearsalPerson.id == person_id)
                             .values(agent_typing_at=datetime.now(timezone.utc) if typing else None))
            await db.commit()


async def set_person_typing(sf, person_id: uuid.UUID, typing: bool) -> None:
    async with sf() as db:
        await db.execute(sa.update(RehearsalPerson).where(RehearsalPerson.id == person_id)
                         .values(agent_typing_at=datetime.now(timezone.utc) if typing else None))
        await db.commit()


def is_typing(person: RehearsalPerson, now: datetime) -> bool:
    shown = _TYPING.get(person.channel, timedelta(seconds=5))
    return person.agent_typing_at is not None and now - person.agent_typing_at < shown


async def thread(sf, person_id: uuid.UUID) -> Sequence[RehearsalMessage]:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalMessage).where(RehearsalMessage.person_id == person_id)
            .order_by(RehearsalMessage.id)
        )).scalars().all()


def with_template_definition(run: RehearsalRun, body: dict[str, Any]) -> dict[str, Any]:
    """A template send, with the template's components as the account defines them."""
    sent = body.get("template") or {}
    language = (sent.get("language") or {}).get("code")
    for template in run.templates or []:
        if template.get("name") == sent.get("name") and template.get("language") in (language, None):
            return {**body, "template": {**sent, "definition": template.get("components") or []}}
    return body


def window_open(messages: Sequence[RehearsalMessage], now: datetime) -> bool:
    """WhatsApp's customer-service window: 24 hours from the person's last message."""
    last_in = max((m.created_at for m in messages if m.direction == "in"), default=None)
    return last_in is not None and now - last_in < _WINDOW


async def put_media(sf, run_id: uuid.UUID, data: bytes, mime_type: str) -> str:
    media_id = f"media{secrets.token_hex(8)}"
    async with sf() as db:
        db.add(RehearsalMedia(id=media_id, run_id=run_id, mime_type=mime_type, data=data))
        await db.commit()
    return media_id


async def get_media(sf, run_id: uuid.UUID, media_id: str) -> RehearsalMedia | None:
    async with sf() as db:
        media = await db.get(RehearsalMedia, media_id)
    return media if media is not None and media.run_id == run_id else None


async def routing_for(sf, kind: str, identifier: str) -> dict[str, Any] | None:
    """The routing record a running rehearsal number resolves to, else ``None``."""
    if kind not in CHANNELS or not is_rehearsal_id(identifier):
        return None
    run = await run_by_number(sf, identifier)
    if run is None or run.status != "running" or run.channel != kind:
        return None
    return {
        "org_id": str(run.org_id), "agent_id": run.agent_id,
        "config": {**(run.routing_config or {}), "rehearsal_run_id": str(run.id)},
    }


async def running_routings(sf, kind: str) -> list[dict[str, Any]]:
    """Every running rehearsal on *kind*, shaped as ops lists a channel's routings."""
    async with sf() as db:
        runs = (await db.execute(sa.select(RehearsalRun).where(
            RehearsalRun.channel == kind, RehearsalRun.status == "running",
        ))).scalars().all()
    return [{"channel_identifier": r.phone_number_id, "org_id": str(r.org_id),
             "config": dict(r.routing_config or {})} for r in runs]


async def end_run(sf, vault, run_id: uuid.UUID) -> RehearsalRun | None:
    async with sf() as db:
        run = await db.get(RehearsalRun, run_id)
        if run is None:
            return None
        run.status, run.ended_at = "ended", datetime.now(timezone.utc)
        await db.commit()
    for name in VAULT_CREDENTIALS[run.channel]:
        await vault.delete(run.org_id, f"{run.channel}_{name}_{run.phone_number_id}")
    return run
