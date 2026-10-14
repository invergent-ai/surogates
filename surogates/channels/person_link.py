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
LINK_TTL_SECONDS = 7 * 24 * 3600


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
