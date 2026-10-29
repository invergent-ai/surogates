"""Runs and their worlds, in the runtime database.

A run's WhatsApp credentials live in the vault under the names the runtime's
WhatsApp adapter already reads (``whatsapp_<cred>_<phone_number_id>``), so the
real adapter serves a rehearsal number exactly as it serves a real one.
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
from surogates.rehearsal.ids import is_rehearsal_id, new_phone_number_id, new_wa_id

VAULT_CREDENTIALS = ("access_token", "app_secret", "verify_token", "phone_number_id", "api_version")

_WINDOW = timedelta(hours=24)
#: WhatsApp drops a typing indicator after 25 seconds, or when a message is sent.
_TYPING = timedelta(seconds=25)

Provision = Callable[..., Awaitable[ResolvedIdentity]]


async def create_run(
    sf, vault, *, org_id: uuid.UUID, agent_id: str, created_by: str,
    routing_config: dict[str, Any], person_name: str,
    provision: Provision = get_or_create_channel_identity,
    templates: list[dict[str, Any]] | None = None,
) -> RehearsalRun:
    run = RehearsalRun(
        org_id=org_id, agent_id=agent_id, created_by=created_by,
        phone_number_id=new_phone_number_id(),
        access_token="EAA" + secrets.token_urlsafe(96), app_secret=secrets.token_hex(16),
        routing_config=dict(routing_config or {}), templates=list(templates or []),
    )
    async with sf() as db:
        db.add(run)
        await db.commit()
    creds = {
        "access_token": run.access_token, "app_secret": run.app_secret,
        "verify_token": secrets.token_hex(8), "phone_number_id": run.phone_number_id,
        "api_version": DEFAULT_API_VERSION,
    }
    for name, value in creds.items():
        await vault.store(org_id, f"whatsapp_{name}_{run.phone_number_id}", value)
    await add_person(sf, run, name=person_name, provision=provision)
    return run


async def add_person(sf, run: RehearsalRun, *, name: str, provision: Provision) -> RehearsalPerson:
    wa_id = new_wa_id()
    # The same call the inbound pipeline makes for an unknown sender: the
    # pipeline then finds this identity, already marked as the run's.
    identity = await provision(
        sf, platform="whatsapp", platform_user_id=wa_id, org_id=run.org_id, display_name=name,
    )
    person = RehearsalPerson(run_id=run.id, name=name, channel="whatsapp", address=wa_id,
                             user_id=identity.user_id)
    async with sf() as db:
        await db.execute(sa.update(User).where(User.id == identity.user_id)
                         .values(rehearsal_run_id=run.id))
        db.add(person)
        await db.commit()
    return person


async def get_run(sf, run_id: uuid.UUID) -> RehearsalRun | None:
    async with sf() as db:
        return await db.get(RehearsalRun, run_id)


async def run_by_number(sf, phone_number_id: str) -> RehearsalRun | None:
    async with sf() as db:
        return (await db.execute(
            sa.select(RehearsalRun).where(RehearsalRun.phone_number_id == phone_number_id)
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


def is_typing(person: RehearsalPerson, now: datetime) -> bool:
    return person.agent_typing_at is not None and now - person.agent_typing_at < _TYPING


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
    if kind != "whatsapp" or not is_rehearsal_id(identifier):
        return None
    run = await run_by_number(sf, identifier)
    if run is None or run.status != "running":
        return None
    return {
        "org_id": str(run.org_id), "agent_id": run.agent_id,
        "config": {**(run.routing_config or {}), "rehearsal_run_id": str(run.id)},
    }


async def end_run(sf, vault, run_id: uuid.UUID) -> RehearsalRun | None:
    async with sf() as db:
        run = await db.get(RehearsalRun, run_id)
        if run is None:
            return None
        run.status, run.ended_at = "ended", datetime.now(timezone.utc)
        await db.commit()
    for name in VAULT_CREDENTIALS:
        await vault.delete(run.org_id, f"whatsapp_{name}_{run.phone_number_id}")
    return run
