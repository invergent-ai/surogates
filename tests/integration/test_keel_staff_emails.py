"""Keel asks keel-link for the mail addresses of an organisation's users, by id.

It answers the addresses of the users who belong to the organisation and whose address mail can reach, and leaves
every other id out.  No address reaches a log or an error.
"""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import OperationalError

from surogates.channels.identity import channel_user_email, get_or_create_channel_identity
from surogates.followup.people import channel_account, named_account
from surogates.tenant.memberships import add_member

from .conftest import create_org, create_user
from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    LINK_KEY,
    code_of,
    link,
    link_app,
    link_headers,
    org,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def mailbox(local: str, *, domain: str = "bank.test") -> str:
    """An address no other test uses: the database is shared within a worker, and an invitation looks everywhere."""
    return f"{local}-{uuid4().hex[:8]}@{domain}"


async def ask(link, org_id, ids, *, key: str | None = LINK_KEY):
    return await link.post(
        f"/v1/keel/orgs/{org_id}/emails",
        json={"user_ids": [str(i) for i in ids]},
        headers=link_headers(key),
    )


async def make_member(session_factory, org_id: UUID, user_id: UUID) -> None:
    async with session_factory() as db:
        await add_member(db, org_id, user_id)
        await db.commit()


async def test_keel_gets_the_address_of_each_user_of_the_org_it_names(link, org, session_factory):
    ana_address = mailbox("Ana.Pop+Loans", domain="Bank.Test")
    dan_address = mailbox("dan")
    ana = await create_user(session_factory, org, email=ana_address)
    dan = await create_user(session_factory, org, email=dan_address)

    response = await ask(link, org, [ana, dan])

    assert response.status_code == 200
    assert response.json() == {"emails": {str(ana): ana_address, str(dan): dan_address}}
    assert response.headers["cache-control"] == "no-store"

    twice = await link.post(
        f"/v1/keel/orgs/{org}/emails",
        json={"user_ids": [str(ana).upper(), str(ana)]},
        headers=link_headers(LINK_KEY),
    )
    assert twice.json() == {"emails": {str(ana): ana_address}}


async def test_only_users_of_the_org_are_answered(link, org, session_factory):
    other = await create_org(session_factory)
    mia_address, ion_address = mailbox("mia"), mailbox("ion")
    mia = await create_user(session_factory, other, email=mia_address)
    await make_member(session_factory, org, mia)
    ion = await create_user(session_factory, other, email=ion_address)

    assert (await ask(link, org, [mia, ion, uuid4()])).json() == {"emails": {str(mia): mia_address}}
    assert (await ask(link, other, [mia, ion])).json() == {"emails": {str(mia): mia_address, str(ion): ion_address}}

    empty = await create_org(session_factory)
    answer = await ask(link, empty, [mia, uuid4()])
    assert answer.status_code == 200
    assert answer.json() == {"emails": {}}


async def test_stand_in_addresses_are_never_answered(link, org, session_factory):
    shadow = (
        await get_or_create_channel_identity(
            session_factory, platform="whatsapp", platform_user_id="40712000103", org_id=org, display_name="Ion",
        )
    ).user_id
    slack_only = await channel_account(
        session_factory, org_id=org, agent_id="sales", platform="slack", platform_user_id="U0123ABC",
        name="Dana", holder=None,
    )
    assert isinstance(slack_only, UUID)
    followed = await named_account(session_factory, org_id=org, agent_id="sales", name="Maria")
    shouting = await create_user(session_factory, org, email=channel_user_email("telegram", "77", org).upper())
    ana_address = mailbox("ana")
    ana = await create_user(session_factory, org, email=ana_address)

    response = await ask(link, org, [shadow, slack_only, followed, shouting, ana])

    assert response.json() == {"emails": {str(ana): ana_address}}


async def test_the_route_refuses_what_it_cannot_answer(link, org, session_factory):
    ana_address = mailbox("ana")
    ana = await create_user(session_factory, org, email=ana_address)
    post = lambda body, path_org=org: link.post(  # noqa: E731
        f"/v1/keel/orgs/{path_org}/emails", json=body, headers=link_headers(LINK_KEY),
    )

    for key in (None, "not-the-key"):
        for path_org in (org, uuid4()):
            response = await ask(link, path_org, [ana], key=key)
            assert response.status_code == 401
            assert code_of(response) == "bad-link-key"

    for body in (
        {},
        {"user_ids": []},
        {"user_ids": None},
        {"user_ids": str(ana)},
        {"user_ids": [""]},
        {"user_ids": ["not-a-uuid"]},
        {"user_ids": [ana_address]},
        {"user_ids": [str(uuid4()) for _ in range(101)]},
        {"user_ids": [str(ana)] * 101},
    ):
        response = await post(body)
        assert response.status_code == 422, body
        assert code_of(response) == "invalid-request"
        assert ana_address not in response.text

    raw = await link.post(f"/v1/keel/orgs/{org}/emails", content=b"not json", headers=link_headers(LINK_KEY))
    assert raw.status_code == 422
    assert code_of(raw) == "invalid-request"

    bad_org = await post({"user_ids": [str(ana)]}, "not-a-uuid")
    assert bad_org.status_code == 422
    assert code_of(bad_org) == "invalid-request"

    unknown = await ask(link, uuid4(), [ana])
    assert unknown.status_code == 404
    assert code_of(unknown) == "unknown-org"

    just_inside = [str(ana)] + [str(uuid4()) for _ in range(99)]
    assert (await post({"user_ids": just_inside})).json() == {"emails": {str(ana): ana_address}}
    assert (await post({"user_ids": [str(ana)], "fields": ["phone"]})).json() == {"emails": {str(ana): ana_address}}


async def test_no_address_reaches_a_log_or_an_error(link, org, session_factory, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    address = mailbox("ana.pop+loans")
    ana = await create_user(session_factory, org, email=address)

    answered = await ask(link, org, [ana])
    assert answered.status_code == 200
    assert address not in "\n".join(r.getMessage() for r in caplog.records)

    async def failing(*_, **__):
        raise OperationalError(
            "SELECT users.email FROM users",
            (address,),
            Exception(f"canceling statement due to statement timeout\nCONTEXT:  {address}"),
        )

    monkeypatch.setattr("surogates.api.routes.keel.org_addresses", failing)
    caplog.clear()

    failed = await ask(link, org, [ana])

    assert failed.status_code == 503
    assert code_of(failed) == "unavailable"
    assert address not in failed.text
    assert address not in caplog.text
    assert "OperationalError" in caplog.text
    assert str(org) in caplog.text
