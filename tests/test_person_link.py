"""A start link made for a known person binds the chat account that opens it."""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest
import sqlalchemy as sa

from surogates.channels import person_link
from surogates.channels.inbound import ChannelInboundPipeline
from surogates.db.models import ChannelIdentity, User
from surogates.session.events import EventType
from tests.test_channel_pipeline import (
    AGENT_ID,
    AGENT_ORG_ID,
    _FakeRedis,
    _make_config,
    _make_deps,
    _make_msg,
    _Routing,
)

ORG = AGENT_ORG_ID
CODE = "Abc_def-12345678"


class _Redis(_FakeRedis):
    async def getdel(self, key):
        return self.kv.pop(key, None)


async def _user(sf, name, provider="whatsapp"):
    async with sf() as db:
        u = User(org_id=ORG, email=f"{name}@x.test", display_name=name, auth_provider=provider)
        db.add(u)
        await db.commit()
        return u.id


async def _identities(sf):
    async with sf() as db:
        return (await db.execute(sa.select(ChannelIdentity))).scalars().all()


def _deps(sf, user_id, *, org=ORG, platform="telegram"):
    redis = _Redis()
    redis.kv[person_link.PREFIX + CODE] = json.dumps(
        {"org_id": str(org), "user_id": str(user_id), "platform": platform},
    )
    return dataclasses.replace(_make_deps(), redis=redis, session_factory=sf)


async def _start(deps, text=f"/start {CODE}"):
    return await ChannelInboundPipeline().handle(
        _make_msg(is_dm=True, platform_user_id="777", text=text),
        routing=_Routing(platform="telegram", identifier="@bot"),
        config=_make_config(), deps=deps,
    )


def test_start_code_reads_only_a_start_with_a_code():
    assert person_link.start_code(f" /start {CODE} ") == CODE
    assert person_link.start_code("/start") is None
    assert person_link.start_code("/start a b") is None
    assert person_link.start_code("hello") is None


def _asking(deps):
    asked = []

    async def private_sender(msg, text, buttons):
        asked.append((text, buttons))
        return True

    return dataclasses.replace(deps, private_sender=private_sender), asked


async def _team(sf):
    """A team member reachable on WhatsApp, and the person's own WhatsApp."""
    from surogates.followup import store

    doctor = await _user(sf, "doctor")
    async with sf() as db:
        db.add(ChannelIdentity(org_id=ORG, user_id=doctor, platform="whatsapp",
                               platform_user_id="40700000099",
                               platform_meta={"contact_permission": {"status": "granted"}}))
        await db.commit()
    await store.update_settings(sf, org_id=ORG, agent_id=AGENT_ID, channel_identifier="pn1",
                                alert_recipient_user_ids=[doctor], message_template_name="msg",
                                message_template_language="ro",
                                texts={"link_question": "Sunteți {name}?", "link_yes": "Da",
                                       "link_no": "Nu"})
    return doctor


async def _outreach(sf):
    from surogates.db.models import OutreachMessageRow

    async with sf() as db:
        return (await db.execute(sa.select(OutreachMessageRow))).scalars().all()


@pytest.mark.asyncio
async def test_start_asks_first_and_links_only_on_yes(sf):
    ana = await _user(sf, "Ana Pop")
    doctor = await _team(sf)
    async with sf() as db:
        db.add(ChannelIdentity(org_id=ORG, user_id=ana, platform="whatsapp",
                               platform_user_id="40711111111",
                               platform_meta={"contact_permission": {"status": "granted"}}))
        await db.commit()
    deps, asked = _asking(_deps(sf, ana))

    await _start(deps)

    assert asked == [("Sunteți Ana Pop?", ["Da", "Nu"])]
    assert [r.platform for r in await _identities(sf)] == ["whatsapp", "whatsapp"]
    assert person_link.PREFIX + CODE not in deps.redis.kv  # single use
    assert not [t for _, t, _d in deps.session_store.events if t == EventType.USER_MESSAGE]

    await _start(deps, text="Da")

    [row] = [r for r in await _identities(sf) if r.platform == "telegram"]
    assert (row.user_id, row.platform_user_id) == (ana, "777")
    perm = row.platform_meta["contact_permission"]
    assert (perm["status"], perm["method"], perm["recorded_by"]) == ("granted", "start_link", "person")
    assert any(c.startswith("channel_identity_changed:telegram") for c, _ in deps.redis.published)
    told = {o.user_id: o for o in await _outreach(sf)}
    assert "Telegram was linked for Ana Pop" in told[doctor].text
    assert told[ana].platform == "whatsapp" and "If this was not you" in told[ana].text
    assert len(asked) == 2  # the confirmation
    texts = [d.get("content") or d.get("text") for _, t, d in deps.session_store.events
             if t == EventType.USER_MESSAGE]
    assert all(CODE not in str(t) for t in texts)


@pytest.mark.asyncio
async def test_no_links_nothing_and_tells_the_team(sf):
    ana = await _user(sf, "Ana Pop")
    doctor = await _team(sf)
    deps, asked = _asking(_deps(sf, ana))

    await _start(deps)
    await _start(deps, text="nu")

    assert not [r for r in await _identities(sf) if r.platform == "telegram"]
    [notice] = await _outreach(sf)
    assert notice.user_id == doctor and "said they are not Ana Pop" in notice.text


def test_a_link_lives_two_days():
    assert person_link.LINK_TTL_SECONDS == 48 * 3600


@pytest.mark.asyncio
async def test_a_code_for_another_org_is_ignored_and_kept(sf):
    ana = await _user(sf, "ana")
    deps = _deps(sf, ana, org=uuid.uuid4())

    await _start(deps)

    assert await _identities(sf) == []
    assert person_link.PREFIX + CODE in deps.redis.kv


@pytest.mark.asyncio
async def test_a_shadow_account_gives_way_but_a_real_person_does_not(sf):
    ana = await _user(sf, "ana")
    shadow = await _user(sf, "777", provider="telegram")
    async with sf() as db:
        db.add(ChannelIdentity(org_id=ORG, user_id=shadow, platform="telegram",
                               platform_user_id="777", platform_meta={}))
        await db.commit()
    assert await person_link.bind(sf, org_id=ORG, user_id=ana, platform="telegram",
                                  platform_user_id="777", display_name="Ana")
    [row] = await _identities(sf)
    assert row.user_id == ana

    dan = await _user(sf, "dan")
    assert not await person_link.bind(sf, org_id=ORG, user_id=dan, platform="telegram",
                                      platform_user_id="777", display_name="Dan")
    [row] = await _identities(sf)
    assert row.user_id == ana


@pytest.mark.asyncio
async def test_relinking_replaces_the_persons_earlier_account(sf):
    ana = await _user(sf, "ana")
    await person_link.bind(sf, org_id=ORG, user_id=ana, platform="telegram",
                           platform_user_id="111", display_name="Ana")
    await person_link.bind(sf, org_id=ORG, user_id=ana, platform="telegram",
                           platform_user_id="222", display_name="Ana")
    assert [r.platform_user_id for r in await _identities(sf)] == ["222"]
