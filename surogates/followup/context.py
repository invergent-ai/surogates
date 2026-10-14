"""Who a follow-up tool call is about, and the wording the tools share.

The follow-up tools and the team tools both answer from here, so a start
date, a time or a notice reads the same whichever tool wrote it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from surogates.channels.capabilities import capabilities_for
from surogates.channels.identity import readable_phone
from surogates.db.models import ChannelIdentity, Session as SessionRow, User
from surogates.followup import store as followup_store
from surogates.followup.eligibility import start_date_problem

__all__ = [
    "Context",
    "channel_names",
    "in_person_zone",
    "load_context",
    "local_time",
    "parse_start_date",
    "start_date_from",
    "start_text",
    "told",
]


_PLATFORM_NAMES = {"whatsapp": "WhatsApp", "slack": "Slack", "telegram": "Telegram"}


def channel_names(platforms: Any) -> str:
    """Channels as people write them: "WhatsApp or Telegram"."""
    return " or ".join(_PLATFORM_NAMES.get(p, p) for p in platforms)


class Context:
    """Who and where the conversation is, read from the session itself."""

    def __init__(self, session: SessionRow, user: User | None) -> None:
        self.session_id: uuid.UUID = session.id
        self.org_id: uuid.UUID = session.org_id
        self.agent_id: str = session.agent_id
        self.user_id: uuid.UUID | None = session.user_id
        self.channel: str = session.channel or ""
        config = session.config or {}
        self.sender: str = str(config.get("channel_identifier") or "")
        self.platform_user_id: str = str(
            config.get(f"{self.channel}_channel_id") or ""
        )
        self.multi_party: bool = bool(config.get("multi_party"))
        self.display_name: str = (user.display_name if user else "") or ""
        #: The enrollment that names the person; read on first use, since
        #: most calls never name anyone.
        self.enrollment: Any = None
        self._named = False

    @property
    def can_write_first(self) -> bool:
        """A one-to-one conversation on a channel the platform may write
        first on, with a sender to write from."""
        return (
            capabilities_for(self.channel).can_initiate
            and not self.multi_party and bool(self.sender) and bool(self.platform_user_id)
        )

    async def person_id(self, session_factory: Any) -> str:
        """The person's own id on this channel.  Not the chat id on Slack,
        where a direct message has an id of its own."""
        if self.user_id is not None:
            async with session_factory() as db:
                found = (
                    await db.execute(
                        sa.select(ChannelIdentity.platform_user_id)
                        .where(ChannelIdentity.org_id == self.org_id)
                        .where(ChannelIdentity.platform == self.channel)
                        .where(ChannelIdentity.user_id == self.user_id)
                        .order_by(ChannelIdentity.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if found:
                return found
        return self.platform_user_id

    def named_by(self, enrollment: Any) -> None:
        self.enrollment, self._named = enrollment, True

    async def person_label(self, session_factory: Any) -> str:
        """How the team recognises who an alert is about."""
        if not self._named and self.user_id is not None:
            self.named_by(followup_store.labelling_enrollment(
                await followup_store.enrollments(
                    session_factory, org_id=self.org_id, agent_id=self.agent_id,
                    user_id=self.user_id,
                )
            ))
        name = followup_store.person_label(self.display_name, self.enrollment)
        parts = [name] if name else []
        if self.channel == "whatsapp" and self.platform_user_id:
            parts.append(readable_phone(self.platform_user_id))
        return " · ".join(parts) or f"user {self.user_id}"


async def load_context(kwargs: dict) -> tuple[Context | None, Any]:
    """The conversation's context and the agent's follow-up settings."""
    sf = kwargs["session_factory"]
    async with sf() as db:
        session = await db.get(SessionRow, uuid.UUID(str(kwargs["session_id"])))
        if session is None:
            return None, None
        user = await db.get(User, session.user_id) if session.user_id else None
        ctx = Context(session, user)
    settings = await followup_store.get_settings(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
    )
    return ctx, settings


def parse_start_date(raw: Any) -> date | None:
    """A YYYY-MM-DD within the protocol horizon, or ValueError."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"start_date must be YYYY-MM-DD, got {text!r}") from exc
    problem = start_date_problem(parsed, datetime.now(timezone.utc).date())
    if problem:
        raise ValueError(problem)
    return parsed


def start_date_from(arguments: dict, timezone_name: str) -> date | None:
    """Day 0 from ``start_date`` or ``days_ago``, never both.

    ``days_ago`` exists because people answer "five days ago", and a model
    left to turn that into a date reaches for a shell to do the arithmetic.
    It counts back from today where the protocol's Program counts days.
    """
    raw_days = arguments.get("days_ago")
    if raw_days is None or (isinstance(raw_days, str) and not raw_days.strip()):
        return parse_start_date(arguments.get("start_date"))
    if str(arguments.get("start_date") or "").strip():
        raise ValueError("give start_date or days_ago, not both")
    if isinstance(raw_days, bool) or not str(raw_days).strip().isdigit():
        raise ValueError(f"days_ago must be a whole number of days, got {raw_days!r}")
    today = datetime.now(ZoneInfo(timezone_name)).date()
    return parse_start_date((today - timedelta(days=int(str(raw_days).strip()))).isoformat())


def start_text(row: Any) -> str:
    if row.start_date_approximate:
        return (
            f"about {row.start_date.isoformat()} (approximate: the person gave "
            f"a rough period; confirm the date when you can)"
        )
    return row.start_date.isoformat()


def in_person_zone(configs: list[dict], enrollment: Any) -> list[dict]:
    """*configs* as they fire for this person: in their own zone, when
    they have one, as materialize asks them."""
    zone = getattr(enrollment, "timezone", None)
    return [{**c, "timezone": zone} for c in configs] if zone else configs


def local_time(at: datetime, config: dict) -> str:
    """*at* in a Program's own zone, the zone named."""
    zone = str(config.get("timezone") or "UTC")
    return f"{at.astimezone(ZoneInfo(zone)):%a %Y-%m-%d %H:%M} {zone}"


def told(queued: int, why_not: str | None) -> str:
    return (
        f"The team was notified ({queued})."
        if queued else f"The team was not notified: {why_not}."
    )
