"""Link a known person to a chat channel through a start link.

An operator asks ops for a link to the agent's bot for one person
(``t.me/<bot>?start=<code>``).  Ops stores the code in Redis under
:data:`PREFIX`; when the person opens the link and presses Start, the bot
receives ``/start <code>`` and :func:`consume` binds that chat account to the
person and records their permission to be messaged there.

The code is single-use and org-bound: a code minted for one agent's org is
ignored by every other org's bot, and left alive for the right one.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from surogates.db.models import ChannelIdentity, User
from surogates.followup.wording import wording

logger = logging.getLogger(__name__)

#: ops writes ``{org_id, user_id, platform}`` here; keep the two in step.
PREFIX = "surogates:person_link:"

# Telegram start parameters are at most 64 of [A-Za-z0-9_-].
_START = re.compile(r"^/start\s+([A-Za-z0-9_-]{8,64})$")


def start_code(text: str | None) -> str | None:
    """The code in a ``/start <code>`` message, else ``None``."""
    m = _START.match((text or "").strip())
    return m.group(1) if m else None


async def bind(
    session_factory: Any,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    display_name: str,
) -> bool:
    """Bind the chat account to the person and record their permission.

    Pressing Start on a link made for them is the person's own opt-in, the
    only one a bot can get, so permission is recorded as theirs
    (``method="start_link"``), not the operator's.  A chat account already
    held by another real person is left alone (``False``); one held by a
    shadow account the runtime provisioned on first contact is taken over.
    """
    async with session_factory() as db:
        row = (
            await db.execute(
                sa.select(ChannelIdentity, User)
                .join(User, ChannelIdentity.user_id == User.id)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.platform_user_id == platform_user_id)
                .with_for_update(of=ChannelIdentity)
            )
        ).first()
        if row is not None and row[0].user_id != user_id and row[1].auth_provider != platform:
            return False
        # The person's earlier account on this channel, if any, gives way.
        await db.execute(
            sa.delete(ChannelIdentity)
            .where(ChannelIdentity.org_id == org_id)
            .where(ChannelIdentity.platform == platform)
            .where(ChannelIdentity.user_id == user_id)
            .where(ChannelIdentity.platform_user_id != platform_user_id)
        )
        identity = row[0] if row is not None else ChannelIdentity(
            id=uuid.uuid4(), org_id=org_id, platform=platform,
            platform_user_id=platform_user_id, platform_meta={},
        )
        identity.user_id = user_id
        meta = dict(identity.platform_meta or {})
        meta.setdefault("display_name", display_name)
        meta["contact_permission"] = {
            "status": "granted",
            "recorded_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "recorded_by": "person",
            "method": "start_link",
        }
        identity.platform_meta = meta
        db.add(identity)
        await db.commit()
    return True


async def consume(
    redis: Any,
    session_factory: Any,
    *,
    code: str,
    org_id: Any,
    platform: str,
    platform_user_id: str,
    display_name: str,
) -> bool:
    """Bind on a valid code for this org and platform; ``True`` when bound."""
    key = f"{PREFIX}{code}"
    raw = await redis.get(key)
    if raw is None:
        return False
    entry = json.loads(raw)
    if str(entry.get("org_id")) != str(org_id) or entry.get("platform") != platform:
        return False
    if await redis.getdel(key) is None:
        return False
    bound = await bind(
        session_factory,
        org_id=uuid.UUID(str(org_id)),
        user_id=uuid.UUID(entry["user_id"]),
        platform=platform,
        platform_user_id=platform_user_id,
        display_name=display_name,
    )
    if bound:
        logger.info("Linked %s:%s to user %s by start link", platform, platform_user_id, entry["user_id"])
        try:
            await redis.publish(
                f"channel_identity_changed:{platform}\x00{platform_user_id}\x00{org_id}", b"",
            )
        except Exception:  # noqa: BLE001 — the cache entry ages out on its own
            logger.warning("channel_identity_changed publish failed", exc_info=True)
    return bound


#: How long a start link stays usable; ops mints its own with the same life.
LINK_TTL_SECONDS = 48 * 3600


async def mint(redis: Any, *, org_id: Any, user_id: Any, platform: str = "telegram") -> str:
    """A one-use code that binds whoever opens the start link to *user_id*,
    stored the way ops stores the ones it mints."""
    import secrets

    code = secrets.token_urlsafe(16)
    await redis.set(
        f"{PREFIX}{code}",
        json.dumps({"org_id": str(org_id), "user_id": str(user_id), "platform": platform}),
        ex=LINK_TTL_SECONDS,
    )
    return code


#: Who opened a start link and has yet to say whether they are its person.
PENDING_PREFIX = "surogates:person_link_pending:"
PENDING_TTL_SECONDS = 24 * 3600

#: The platform's own wording; ``followup_settings.texts`` overrides each key
#: in the business's language.
DEFAULT_TEXTS = {
    "link_question": "Are you {name}?",
    "link_yes": "Yes",
    "link_no": "No",
    "link_done": "Thank you, {name}: you are linked and will get your messages here.",
    "link_refused": "Thank you. This link was not meant for you, so nothing was linked.",
    "link_notice_person": (
        "Your follow-up was just linked to a Telegram account ({account}). If "
        "this was not you, please tell us."
    ),
    "link_notice_team": "Telegram was linked for {name} ({account}).",
    "link_refused_team": (
        "Someone opened {name}'s Telegram link ({account}) and said they are "
        "not {name}: nothing was linked."
    ),
}
_YES = {"yes", "da", "y", "ja", "oui", "si", "sí", "igen"}
_NO = {"no", "nu", "n", "nein", "non", "nem"}


def _pending_key(org_id: Any, platform: str, platform_user_id: str) -> str:
    return f"{PENDING_PREFIX}{org_id}:{platform}:{platform_user_id}"


async def _texts(session_factory: Any, org_id: Any, agent_id: str) -> tuple[Any, dict]:
    from surogates.followup import store as followup_store

    settings = await followup_store.get_settings(
        session_factory, org_id=uuid.UUID(str(org_id)), agent_id=agent_id,
    )
    return settings, (getattr(settings, "texts", None) or {})


async def start(
    redis: Any, session_factory: Any, *, code: str, org_id: Any, agent_id: str,
    platform: str, platform_user_id: str,
) -> tuple[str, list[str]] | None:
    """Someone opened a start link: the question to ask them first
    ("Are you …?") and its two answers, or ``None`` for a code that is not
    live for this org and platform.  Nothing is linked until they say yes.
    """
    key = f"{PREFIX}{code}"
    raw = await redis.get(key)
    if raw is None:
        return None
    entry = json.loads(raw)
    if str(entry.get("org_id")) != str(org_id) or entry.get("platform") != platform:
        return None
    if await redis.getdel(key) is None:
        return None
    from surogates.followup import store as followup_store

    name = await followup_store.person_name(
        session_factory, org_id=uuid.UUID(str(org_id)), agent_id=agent_id,
        user_id=uuid.UUID(entry["user_id"]),
    ) or "?"
    await redis.set(
        _pending_key(org_id, platform, platform_user_id),
        json.dumps({"user_id": entry["user_id"], "name": name}), ex=PENDING_TTL_SECONDS,
    )
    _settings, texts = await _texts(session_factory, org_id, agent_id)
    return (
        wording(texts, "link_question", name=name),
        [wording(texts, "link_yes"), wording(texts, "link_no")],
    )


async def answer(
    redis: Any, session_factory: Any, *, org_id: Any, agent_id: str, platform: str,
    platform_user_id: str, display_name: str, text: str | None,
) -> str | None:
    """The reply to the start link's question, when one is waiting and this
    is a yes or a no: what to say back.  Yes links the account and tells the
    person on their other channels and the team; no links nothing and tells
    the team, so a forwarded link that was misused gets noticed.  ``None``
    when nothing is waiting or the message is neither.
    """
    pending_key = _pending_key(org_id, platform, platform_user_id)
    raw = await redis.get(pending_key)
    if raw is None:
        return None
    settings, texts = await _texts(session_factory, org_id, agent_id)
    said = (text or "").strip().rstrip(".!").casefold()
    yes = said in _YES | {wording(texts, "link_yes").casefold()}
    no = said in _NO | {wording(texts, "link_no").casefold()}
    if not (yes or no) or await redis.getdel(pending_key) is None:
        return None
    entry = json.loads(raw)
    name, user_id = entry.get("name") or "?", uuid.UUID(entry["user_id"])
    account = display_name or platform_user_id
    org = uuid.UUID(str(org_id))
    from surogates.followup import store as followup_store

    if no:
        await followup_store.notify_team(
            session_factory, settings, org_id=org, agent_id=agent_id, kind="alert",
            text=wording(texts, "link_refused_team", name=name, account=account),
            created_by="start-link", subject_user_id=user_id, topic="attention",
        )
        return wording(texts, "link_refused")
    bound = await bind(
        session_factory, org_id=org, user_id=user_id, platform=platform,
        platform_user_id=platform_user_id, display_name=display_name,
    )
    if not bound:
        return wording(texts, "link_refused")
    try:
        await redis.publish(
            f"channel_identity_changed:{platform}\x00{platform_user_id}\x00{org_id}", b"",
        )
    except Exception:  # noqa: BLE001 — the cache entry ages out on its own
        logger.warning("channel_identity_changed publish failed", exc_info=True)
    await _tell_person_elsewhere(
        session_factory, settings, org=org, agent_id=agent_id, user_id=user_id,
        platform=platform, text=wording(texts, "link_notice_person", name=name, account=account),
    )
    await followup_store.notify_team(
        session_factory, settings, org_id=org, agent_id=agent_id, kind="notice",
        text=wording(texts, "link_notice_team", name=name, account=account),
        created_by="start-link", subject_user_id=user_id, topic="attention",
    )
    return wording(texts, "link_done", name=name)


async def _tell_person_elsewhere(
    session_factory: Any, settings: Any, *, org: uuid.UUID, agent_id: str,
    user_id: uuid.UUID, platform: str, text: str,
) -> None:
    """The person hears of the link on another channel they can be reached on."""
    from surogates.followup import people
    from surogates.followup import store as followup_store

    template = getattr(settings, "message_template_name", None)
    found, _why = await people.reachable(
        session_factory, org_id=org, user_id=user_id,
        senders=followup_store.senders_of(settings), template=bool(template),
        now=datetime.now(timezone.utc),
    )
    other = next((r for r in found if r.platform != platform), None)
    if other is None:
        return
    await followup_store.queue_outreach(
        session_factory, org_id=org, agent_id=agent_id, user_id=user_id,
        platform=other.platform, channel_identifier=other.sender, kind="notice", text=text,
        template_name=template if other.needs_template else None,
        template_language=settings.message_template_language if other.needs_template else None,
        subject_user_id=user_id, created_by="start-link",
    )
