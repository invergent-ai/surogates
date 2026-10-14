"""Accounts for people the team or a contact names, and how to reach them.

The team tools and the follow-up tools both create people who have not
written yet (a followed person without a phone, a relative's number, a Slack
member) and both need the one channel a message can go out on now.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, NamedTuple

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from surogates.channels.capabilities import capabilities_for
from surogates.channels.identity import channel_user_email, masked_phone
from surogates.db.agent_users import SOURCE_MANUAL, ensure_agent_user
from surogates.db.models import ChannelIdentity, User
from surogates.followup import store as followup_store
from surogates.followup.eligibility import as_utc
from surogates.followup.outreach import window_open

__all__ = [
    "STOP_WORDS",
    "Reach",
    "is_stop",
    "withdraw_consent",
    "channel_account",
    "channel_holder",
    "named_account",
    "reachable",
]

_NAMES = {"whatsapp": "WhatsApp", "slack": "Slack", "telegram": "Telegram"}


async def channel_holder(sf: Any, org_id: uuid.UUID, platform: str, platform_user_id: str) -> User | None:
    """The account this number or member id already belongs to, the way ops
    resolves it: the identity holding it, else the channel-only account's
    e-mail.  Exact match only, never by name."""
    async with sf() as db:
        held = (
            await db.execute(
                sa.select(ChannelIdentity.user_id)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.platform_user_id == platform_user_id)
            )
        ).scalars().first()
        if held is not None:
            return await db.get(User, held)
        return (
            await db.execute(
                sa.select(User)
                .where(User.org_id == org_id)
                .where(sa.func.lower(User.email) == channel_user_email(platform, platform_user_id, org_id))
                .order_by(User.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()


async def channel_account(
    sf: Any, *, org_id: uuid.UUID, agent_id: str, platform: str, platform_user_id: str,
    name: str, holder: User | None,
) -> uuid.UUID | str:
    """The account behind a number or member id, created when there is none,
    in the shape ops' Users page creates: synthetic e-mail, external auth, the
    id bound, a runtime shadow promoted, and bound to this agent.  A string
    says why not."""
    async with sf() as db:
        user = await db.get(User, holder.id) if holder is not None else None
        if user is None:
            user = User(
                org_id=org_id, display_name=name, auth_provider="external",
                email=channel_user_email(platform, platform_user_id, org_id),
            )
            db.add(user)
            await db.flush()
        bound = (
            await db.execute(
                sa.select(ChannelIdentity.platform_user_id)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.user_id == user.id)
            )
        ).scalars().first()
        if bound is None:
            db.add(ChannelIdentity(
                org_id=org_id, user_id=user.id, platform=platform,
                platform_user_id=platform_user_id, platform_meta={},
            ))
        elif bound != platform_user_id:
            # Never move a person's number, as the Users page refuses to.
            shown = masked_phone(bound) if platform == "whatsapp" else f"…{bound[-4:]}"
            return f"that person's account already has the {_NAMES.get(platform, platform)} id {shown}."
        # Someone who wrote first has a runtime shadow account, which the
        # linked identity policy refuses; being vouched for makes them known.
        if user.auth_provider == platform:
            user.auth_provider = "external"
        await ensure_agent_user(
            db, org_id=org_id, agent_id=agent_id, user_id=user.id,
            source=SOURCE_MANUAL, reactivate=True,
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return "the id was registered for someone else at the same moment; try again."
        return user.id


async def named_account(sf: Any, *, org_id: uuid.UUID, agent_id: str, name: str) -> uuid.UUID:
    """A followed person known only by name: no phone or chat account yet.
    Their contacts are reached for them until they have one."""
    user_id = uuid.uuid4()
    async with sf() as db:
        db.add(User(
            id=user_id, org_id=org_id, display_name=name, auth_provider="external",
            email=f"person-{user_id}@{org_id}.people.surogate.local",
        ))
        await db.flush()
        await ensure_agent_user(
            db, org_id=org_id, agent_id=agent_id, user_id=user_id,
            source=SOURCE_MANUAL, reactivate=True,
        )
        await db.commit()
    return user_id


class Reach(NamedTuple):
    """A channel a message can go out on now."""

    platform: str
    identity: Any
    sender: str
    last_inbound: datetime | None
    #: The provider's window is closed: the approved template goes first.
    needs_template: bool


async def reachable(
    sf: Any, *, org_id: uuid.UUID, user_id: uuid.UUID, senders: dict[str, str],
    preferred: str | None = None, template: bool = False, now: datetime,
) -> tuple[list[Reach], str | None]:
    """Every channel a message to *user_id* can go out on now, best first:
    *preferred*, then the one they last wrote on, then WhatsApp.  A channel
    whose window is closed counts only when *template* (an approved template
    is chosen).  When none can, why, in words for the team."""
    async with sf() as db:
        found = (
            await db.execute(
                sa.select(ChannelIdentity)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.user_id == user_id)
                .order_by(ChannelIdentity.id.desc())
            )
        ).scalars().all()
    identities: dict[str, Any] = {}
    for identity in found:
        if capabilities_for(identity.platform).can_initiate:
            identities.setdefault(identity.platform, identity)
    if not identities:
        return [], "has no number or account the business can write to"
    granted = {
        p: i for p, i in identities.items() if followup_store.permission_status(i) == "granted"
    }
    if not granted:
        return [], f"has not given permission to be messaged on {_names(identities)}"
    with_sender = [p for p in granted if senders.get(p)]
    if not with_sender:
        return [], f"{_names(granted)} is not switched on for this business"
    out: list[Reach] = []
    why: str | None = None
    for platform in with_sender:
        caps = capabilities_for(platform)
        last = await followup_store.last_inbound(
            sf, org_id=org_id, platform=platform,
            platform_user_id=granted[platform].platform_user_id, channel_identifier=senders[platform],
        )
        if caps.needs_prior_contact and last is None:
            why = why or f"has never written to the business on {_names([platform])}"
            continue
        closed = bool(caps.window_hours) and not window_open(last, now)
        if closed and not template:
            why = why or (
                f"has not written in the last {caps.window_hours} hours, so "
                f"{_names([platform])} only lets an approved template reach them, "
                "and none is chosen"
            )
            continue
        out.append(Reach(platform, granted[platform], senders[platform], last, closed))
    out.sort(key=lambda r: (
        r.platform != preferred,
        -(as_utc(r.last_inbound).timestamp() if r.last_inbound else 0),
        r.platform != "whatsapp",
    ))
    return out, (None if out else why)


def _names(platforms: Any) -> str:
    return " or ".join(_NAMES.get(p, p) for p in platforms)


#: Whole messages that mean "stop writing to me", whatever the agent makes of
#: them; folded to lower case without trailing punctuation.
STOP_WORDS = frozenset({
    "stop", "stop all", "unsubscribe", "opt out", "optout", "cancel messages",
    "dezabonare", "dezabonează-mă", "dezaboneaza-ma", "opreste", "oprește",
    "stop mesaje", "nu mai trimiteți", "nu mai trimiteti",
})


def is_stop(text: str | None) -> bool:
    return (text or "").strip().rstrip(".!").strip().casefold() in STOP_WORDS


async def withdraw_consent(
    sf: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID, platform: str,
    platform_user_id: str, reason: str, by: str,
) -> tuple[bool, list[str], tuple[int, str | None]]:
    """Stop messaging *user_id* on *platform*: their permission there is
    withdrawn, and when they are a followed person their active follow-up
    ends.  A contact only stops receiving it.  The team is told once.

    Returns (whether anything changed, the journeys ended, (told, why not)).
    Idempotent: asking twice changes nothing the second time.
    """
    async with sf() as db:
        identity = (
            await db.execute(
                sa.select(ChannelIdentity)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == platform)
                .where(ChannelIdentity.platform_user_id == platform_user_id)
            )
        ).scalars().first()
    already = identity is None or followup_store.permission_status(identity) == "withdrawn"
    if not already:
        await followup_store.grant_contact_permission(
            sf, org_id=org_id, platform=platform, platform_user_id=platform_user_id,
            recorded_by=by, method="opt_out", status="withdrawn",
        )
    channel = _names([platform])
    ended = []
    for row in await followup_store.enrollments(
        sf, org_id=org_id, agent_id=agent_id, user_id=user_id, status="active",
    ):
        await followup_store.update_enrollment(
            sf, org_id=org_id, agent_id=agent_id, user_id=user_id, protocol=row.protocol,
            status="ended", status_reason=f"Asked to stop on {channel}: {reason}"[:500],
        )
        ended.append(row.protocol)
    if already and not ended:
        return False, [], (0, None)
    settings = await followup_store.get_settings(sf, org_id=org_id, agent_id=agent_id)
    speaker = await followup_store.speaker_for(sf, org_id=org_id, agent_id=agent_id, user_id=user_id)
    w = followup_store.wording
    if speaker is not None and speaker.role == "contact":
        who = w(settings, "label_contact_for", contact=speaker.name or w(settings, "label_contact"),
                relation=speaker.relation or w(settings, "label_contact"), name=speaker.person_name)
    elif speaker is not None:
        who = speaker.person_name
    else:
        who = (await followup_store.display_names(sf, [user_id])).get(user_id) or "?"
    what = (
        w(settings, "stop_ended", journeys=", ".join(ended)) if ended
        else w(settings, "stop_contact")
    )
    text = w(settings, "notice_stopped", who=who, channel=channel, what=what, reason=reason).strip()
    told = await followup_store.notify_team(
        sf, settings, org_id=org_id, agent_id=agent_id, kind="alert", text=text,
        reason=text, created_by=by, subject_user_id=speaker.person_user_id if speaker else user_id,
        topic="attention", exclude_user_id=user_id,
        dedupe_prefix=f"stop:{platform}:{platform_user_id}:{datetime.now():%Y-%m-%d}",
    )
    return True, ended, told
