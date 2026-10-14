"""Tools for the people around someone followed: their contacts, and stopping.

Like the other follow-up tools these act only for the conversation's own
person: the person followed, or one of their confirmed contacts acting for
them.  Nobody is ever linked on a name alone: a number a linked person gives
links on an exact match, and a newcomer's claim waits until a linked person
confirms it in their own chat.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa

from surogates.channels.identity import masked_phone, normalize_wa_id, readable_phone
from surogates.db.models import ChannelIdentity
from surogates.followup import people
from surogates.followup import store as followup_store
from surogates.followup.context import Context, channel_names, load_context, told
from surogates.session.interactive_input import fold_label
from surogates.tools.registry import ToolSchema

CONTACT_TOOL_NAMES = frozenset({"followup_contacts", "followup_withdraw_consent"})

FOLLOWUP_CONTACTS_SCHEMA = ToolSchema(
    name="followup_contacts",
    description=(
        "The people linked to the person followed (relatives, carers). Each "
        "linked contact receives every scheduled step on their own channel, an "
        "answer from any of them answers it for all, and they share the "
        "person's record. Actions: list; add (a number the person or a linked "
        "contact gives you, with who they are to the person); remove; consent "
        "(the contact writing agreed to receive the messages here); claim (the "
        "one writing is not linked yet and says who they are to someone "
        "followed: a linked person is asked to confirm in their own chat, or "
        "the team if nobody can be asked); confirm (a linked person answered "
        "whether a number belongs to their relative). Never link anyone on a "
        "name alone, and never on the newcomer's word alone."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "remove", "consent", "claim", "confirm"],
            },
            "phone": {
                "type": "string",
                "description": "add/remove/confirm: the contact's number with country code.",
            },
            "name": {"type": "string", "description": "add: the contact's name."},
            "relation": {
                "type": "string",
                "description": (
                    "add/claim: who the contact is to the person followed, in the "
                    "words given, e.g. \"daughter\"."
                ),
            },
            "person_name": {
                "type": "string",
                "description": "claim: the full name of the person they say they are linked to.",
            },
            "person_phone": {
                "type": "string",
                "description": "claim: that person's number, when the one writing gave it.",
            },
            "answer": {
                "type": "boolean",
                "description": "confirm: true when the linked person said yes, false for no.",
            },
        },
        "required": ["action"],
    },
)

FOLLOWUP_WITHDRAW_CONSENT_SCHEMA = ToolSchema(
    name="followup_withdraw_consent",
    description=(
        "Stop the business's messages to the one writing, on this channel, the "
        "moment they ask: stop, unsubscribe, do not write to me, in any "
        "language. It records the withdrawal; for the person followed their "
        "follow-up ends, a contact just stops receiving it. The team is told. "
        "Then confirm to them in one sentence that no more messages will come "
        "here, and do not try to change their mind."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "What they said, briefly, in the team's language.",
            },
        },
        "required": ["reason"],
    },
)


def _phone(raw: Any) -> str | None:
    try:
        return normalize_wa_id(str(raw or ""))
    except ValueError:
        return None


async def _whatsapp_of(sf: Any, org_id: uuid.UUID, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not user_ids:
        return {}
    async with sf() as db:
        rows = (
            await db.execute(
                sa.select(ChannelIdentity.user_id, ChannelIdentity.platform_user_id)
                .where(ChannelIdentity.org_id == org_id)
                .where(ChannelIdentity.platform == "whatsapp")
                .where(ChannelIdentity.user_id.in_(user_ids))
            )
        ).all()
    return {uid: pid for uid, pid in rows}


async def _linked(sf: Any, ctx: Context) -> Any:
    """The writer as the person or a confirmed contact, else ``None``."""
    return await ctx.speaker(sf)


async def _list(sf: Any, ctx: Context, speaker: Any) -> str:
    links = await followup_store.contacts(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
        person_user_id=speaker.person_user_id, status=None,
    )
    if not links:
        return f"{speaker.person_name or 'The person'} has no linked contacts."
    ids = [c.contact_user_id for c in links]
    names = await followup_store.display_names(sf, ids)
    phones = await _whatsapp_of(sf, ctx.org_id, ids)
    lines = [f"Contacts of {speaker.person_name or 'the person'}:"]
    for c in links:
        phone = phones.get(c.contact_user_id)
        lines.append(
            f"- {names.get(c.contact_user_id) or '?'} ({c.relation or 'contact'})"
            + (f", {masked_phone(phone)}" if phone else "")
            + (", waiting for confirmation" if c.status == "pending" else "")
        )
    return "\n".join(lines)


async def add_contact(
    sf: Any, ctx: Context, settings: Any, person_user_id: uuid.UUID, person_name: str,
    arguments: dict,
) -> str:
    """Link the number in *arguments* to the person, as one already linked
    gave it: an exact match links that account, a new number gets one."""
    wa_id = _phone(arguments.get("phone"))
    if wa_id is None:
        return "Not linked: give the contact's number with its country code."
    relation = str(arguments.get("relation") or "").strip()
    if not relation:
        return "Not linked: ask who they are to the person followed."
    holder = await people.channel_holder(sf, ctx.org_id, "whatsapp", wa_id)
    if holder is not None and followup_store.is_team_member(settings, holder.id):
        return f"Not linked: {masked_phone(wa_id)} belongs to a member of the team."
    if holder is not None and holder.id == person_user_id:
        return "Not linked: that is the person's own number."
    name = str(arguments.get("name") or "").strip() or relation
    account = await people.channel_account(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, platform="whatsapp",
        platform_user_id=wa_id, name=name, holder=holder,
    )
    if isinstance(account, str):
        return f"Not linked: {account}"
    await followup_store.link_contact(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, person_user_id=person_user_id,
        contact_user_id=account, relation=relation, created_by=f"contact:{ctx.user_id}",
    )
    return (
        f"Linked {name} ({relation}, {masked_phone(wa_id)}) to "
        f"{person_name or 'the person'}. They receive the scheduled steps "
        "once they agree to messages from the business: until they write and "
        "agree, they are skipped."
    )


async def _remove(sf: Any, ctx: Context, speaker: Any, arguments: dict) -> str:
    links = await followup_store.contacts(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
        person_user_id=speaker.person_user_id, status=None,
    )
    wa_id = _phone(arguments.get("phone"))
    phones = await _whatsapp_of(sf, ctx.org_id, [c.contact_user_id for c in links])
    if wa_id:
        found = [c for c in links if phones.get(c.contact_user_id) == wa_id]
    else:
        names = await followup_store.display_names(sf, [c.contact_user_id for c in links])
        needle = fold_label(str(arguments.get("name") or "").strip())
        found = [c for c in links if needle and fold_label(names.get(c.contact_user_id, "")) == needle]
    if len(found) != 1:
        return "Not removed: say which contact by their number, or their exact name. " + await _list(sf, ctx, speaker)
    await followup_store.remove_contact(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
        person_user_id=speaker.person_user_id, contact_user_id=found[0].contact_user_id,
    )
    return "Removed: they no longer receive the person's messages or see their record."


async def _claimed_person(sf: Any, ctx: Context, settings: Any, arguments: dict) -> uuid.UUID | str:
    """The one followed person a newcomer named: by their number when given,
    else by their exact name among the people followed; only ever to know
    whom to ask."""
    rows = [
        r for r in await followup_store.enrollments(sf, org_id=ctx.org_id, agent_id=ctx.agent_id)
        if not followup_store.is_team_member(settings, r.user_id)
    ]
    wa_id = _phone(arguments.get("person_phone"))
    if wa_id:
        holder = await people.channel_holder(sf, ctx.org_id, "whatsapp", wa_id)
        if holder is not None and any(r.user_id == holder.id for r in rows):
            return holder.id
    needle = fold_label(str(arguments.get("person_name") or "").strip())
    if not needle:
        return "ask for the full name of the person they are linked to"
    names = await followup_store.display_names(sf, {r.user_id for r in rows})
    found = {
        r.user_id for r in rows
        if needle in (fold_label(r.patient_name or ""), fold_label(names.get(r.user_id, "")))
    }
    if len(found) == 1:
        return found.pop()
    return "nobody followed matches that name" if not found else "several people followed have that name"


async def _claim(sf: Any, ctx: Context, settings: Any, arguments: dict) -> str:
    relation = str(arguments.get("relation") or "").strip()
    if not relation:
        return "Not done: ask who they are to the person followed."
    if not ctx.can_write_first:
        return "Not done: a link can only be asked for from a chat the business can write back to."
    person = await _claimed_person(sf, ctx, settings, arguments)
    writer = readable_phone(ctx.platform_user_id) if ctx.channel == "whatsapp" else ctx.display_name
    if isinstance(person, str):
        return await _to_team(sf, ctx, settings, f"{person}; claims to be {relation} of "
                              f"{arguments.get('person_name') or '?'}", None)
    linked = await followup_store.contacts(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, person_user_id=person,
    )
    if ctx.user_id == person or any(c.contact_user_id == ctx.user_id for c in linked):
        return "Already linked: nothing to confirm."
    name = await followup_store.person_name(sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=person)
    senders = followup_store.senders_of(settings)
    if ctx.sender and ctx.channel not in senders:
        senders[ctx.channel] = ctx.sender
    template = bool(settings and settings.message_template_name)
    now = datetime.now(timezone.utc)
    for candidate in [person, *(c.contact_user_id for c in linked)]:
        found, _why = await people.reachable(
            sf, org_id=ctx.org_id, user_id=candidate, senders=senders, template=template, now=now,
        )
        if not found:
            continue
        reach = found[0]
        await followup_store.link_contact(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, person_user_id=person,
            contact_user_id=ctx.user_id, relation=relation, status="pending",
            asked_user_id=candidate, created_by=f"claim:{ctx.user_id}",
        )
        await followup_store.queue_outreach(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=candidate,
            platform=reach.platform, channel_identifier=reach.sender, kind="notice",
            text=(
                f"{ctx.display_name or 'Someone'} ({writer}) says they are {relation} of "
                f"{name} and asks to receive {name}'s messages too. Is {writer} really "
                f"their number? Please answer yes or no."
            ),
            template_name=settings.message_template_name if reach.needs_template else None,
            template_language=settings.message_template_language if reach.needs_template else None,
            subject_user_id=person, created_by="contact-claim",
            dedupe_key=f"claim:{ctx.agent_id}:{person}:{ctx.user_id}",
        )
        return (
            "Asked a person already linked to confirm, in their own chat. Until "
            "they say yes this person is not linked: tell them so, and share "
            "nothing about the person followed meanwhile."
        )
    return await _to_team(sf, ctx, settings, f"claims to be {relation} of {name}", person)


async def _to_team(sf: Any, ctx: Context, settings: Any, what: str, person: Any) -> str:
    """Nobody can confirm in chat: the team decides."""
    writer = readable_phone(ctx.platform_user_id) if ctx.channel == "whatsapp" else ctx.display_name
    text = f"Link request: {ctx.display_name or 'someone'} ({writer}) {what}. Confirm or refuse it."
    await followup_store.record_escalation(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=person or ctx.user_id,
        kind="link_request", reason=text, platform=ctx.channel or None, session_id=ctx.session_id,
    )
    queued, why_not = await followup_store.notify_team(
        sf, settings, org_id=ctx.org_id, agent_id=ctx.agent_id, kind="alert", text=text,
        reason=text, created_by="agent", subject_user_id=person or ctx.user_id,
        topic="attention", exclude_user_id=ctx.user_id,
        fallback_senders={ctx.channel: ctx.sender},
    )
    return (
        "Not linked: nobody linked to that person can be asked in chat, so the "
        f"team was asked to confirm. {told(queued, why_not)} Tell them the team "
        "will confirm, and share nothing about the person followed meanwhile."
    )


async def _confirm(sf: Any, ctx: Context, speaker: Any, arguments: dict) -> str:
    if not isinstance(arguments.get("answer"), bool):
        return "Not done: pass answer true or false, as the linked person said."
    pending = await followup_store.contacts(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
        person_user_id=speaker.person_user_id, status="pending",
    )
    wa_id = _phone(arguments.get("phone"))
    if wa_id:
        phones = await _whatsapp_of(sf, ctx.org_id, [c.contact_user_id for c in pending])
        pending = [c for c in pending if phones.get(c.contact_user_id) == wa_id]
    if len(pending) != 1:
        return (
            "Nothing to confirm: no request is waiting" if not pending
            else "Several requests are waiting: pass the phone of the one they answered about"
        ) + "."
    link = pending[0]
    if arguments["answer"]:
        await followup_store.link_contact(
            sf, org_id=ctx.org_id, agent_id=ctx.agent_id, person_user_id=link.person_user_id,
            contact_user_id=link.contact_user_id, relation=link.relation,
            created_by=f"confirmed:{ctx.user_id}",
        )
        return (
            "Linked: they now receive the person's steps (once they agree to "
            "messages) and share the person's record."
        )
    await followup_store.remove_contact(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id,
        person_user_id=link.person_user_id, contact_user_id=link.contact_user_id,
    )
    return "Refused: the request is dropped and they are not linked."


async def handle_followup_contacts(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, settings = await load_context(kwargs)
    if ctx is None or ctx.user_id is None:
        return "Contacts need a conversation with a known person."
    if followup_store.is_team_member(settings, ctx.user_id):
        return "This person is on the team; the team tools manage people."
    action = str(arguments.get("action") or "").strip()
    if action == "claim":
        return await _claim(sf, ctx, settings, arguments)
    speaker = await _linked(sf, ctx)
    if speaker is None:
        return (
            "Not done: the one writing is not followed or linked to anyone "
            "followed. If they say they are someone's relative, use action claim."
        )
    if action == "list":
        return await _list(sf, ctx, speaker)
    if action == "add":
        return await add_contact(
            sf, ctx, settings, speaker.person_user_id, speaker.person_name, arguments,
        )
    if action == "remove":
        return await _remove(sf, ctx, speaker, arguments)
    if action == "confirm":
        return await _confirm(sf, ctx, speaker, arguments)
    if action == "consent":
        if not ctx.can_write_first:
            return f"Not done: consent is recorded in a chat on {channel_names(['whatsapp', 'telegram', 'slack'])}."
        await followup_store.grant_contact_permission(
            sf, org_id=ctx.org_id, platform=ctx.channel,
            platform_user_id=await ctx.person_id(sf), recorded_by="agent",
        )
        return "Consent recorded: they receive the scheduled steps here from now on."
    return "action must be list, add, remove, consent, claim or confirm."


async def handle_followup_withdraw_consent(arguments: dict, **kwargs: Any) -> str:
    sf = kwargs["session_factory"]
    ctx, _settings = await load_context(kwargs)
    if ctx is None or ctx.user_id is None or not ctx.channel:
        return "No known person in this conversation."
    reason = str(arguments.get("reason") or "").strip() or "asked to stop"
    changed, ended, (queued, why_not) = await people.withdraw_consent(
        sf, org_id=ctx.org_id, agent_id=ctx.agent_id, user_id=ctx.user_id,
        platform=ctx.channel, platform_user_id=await ctx.person_id(sf),
        reason=reason, by="agent",
    )
    done = (
        f"Done: no more messages to them on {channel_names([ctx.channel])}"
        + (f"; {', '.join(ended)} ended" if ended else "")
        + "."
    )
    if changed:
        done += " " + told(queued, why_not)
    return done + " Confirm it to them in one sentence."
