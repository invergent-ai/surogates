"""A user in several organisations: memberships, the tokens they accept, and ending them."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from surogates.cli.main import cli_main
from surogates.db.agent_users import purge_user_account
from surogates.db.models import OrgMember, Session, User
from surogates.tenant.auth.jwt import create_access_token, create_refresh_token
from surogates.tenant.memberships import (
    MembershipError,
    MembershipsRemain,
    add_member,
    remove_member,
)

from .conftest import create_org, create_user
from .test_devices import api  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def token_for(org_id: UUID, user_id: UUID) -> str:
    return create_access_token(org_id, user_id, {"sessions:read", "sessions:write"}, auth_time=int(time.time()))


def org(org_id: UUID, name: str | None = None) -> dict:
    return {"id": str(org_id), "name": name or f"test-org-{org_id}"}


async def me(api, token: str):
    return await api.client.get("/v1/auth/me", headers=api.auth(token))


async def add(session_factory, org_id: UUID, user_id: UUID) -> bool:
    async with session_factory() as db:
        added = await add_member(db, org_id, user_id)
        await db.commit()
    return added


async def remove(session_factory, org_id: UUID, user_id: UUID) -> bool:
    async with session_factory() as db:
        removed = await remove_member(db, org_id, user_id)
        await db.commit()
    return removed


async def memberships_of(session_factory, org_id: UUID) -> int:
    async with session_factory() as db:
        return await db.scalar(select(func.count()).select_from(OrgMember).where(OrgMember.org_id == org_id))


async def test_a_member_reads_every_organisation_they_belong_to(api, session_factory):
    org_b = await create_org(session_factory)
    # Named to sort before ORG_B: the organisations after the home one come by name.
    acme = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'Acme')"), {"id": acme})
        await db.commit()
    assert await add(session_factory, org_b, api.user_id) is True
    assert await add(session_factory, acme, api.user_id) is True
    every = [org(api.org_id), org(acme, "Acme"), org(org_b)]

    home = await me(api, api.token)
    assert home.status_code == 200, home.text
    assert home.json()["org_id"] == str(api.org_id)
    assert home.json()["organisations"] == every

    through_b = await me(api, token_for(org_b, api.user_id))
    assert through_b.status_code == 200, through_b.text
    assert through_b.json()["org_id"] == str(org_b)
    assert through_b.json()["org_name"] == f"test-org-{org_b}"
    assert through_b.json()["organisations"] == every


async def test_a_token_stops_working_once_its_membership_ends(api, session_factory):
    org_b = await create_org(session_factory)
    await add(session_factory, org_b, api.user_id)
    token_b = token_for(org_b, api.user_id)
    assert (await me(api, token_b)).status_code == 200

    assert await remove(session_factory, org_b, api.user_id) is True

    refused = await me(api, token_b)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "User not found."
    home = await me(api, api.token)
    assert home.status_code == 200, home.text
    assert home.json()["organisations"] == [org(api.org_id)]


async def test_a_removed_member_cannot_refresh_into_the_organisation(api, session_factory):
    org_b = await create_org(session_factory)
    await add(session_factory, org_b, api.user_id)
    refresh_token = create_refresh_token(org_b, api.user_id)

    renewed = await api.client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert renewed.status_code == 200, renewed.text
    assert (await me(api, renewed.json()["access_token"])).json()["org_id"] == str(org_b)

    await remove(session_factory, org_b, api.user_id)

    refused = await api.client.post("/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refused.status_code == 401
    assert refused.json()["detail"] == "User not found."


async def test_a_token_of_an_organisation_the_user_never_joined_is_refused(api, session_factory):
    org_b = await create_org(session_factory)

    refused = await me(api, token_for(org_b, api.user_id))

    assert refused.status_code == 401
    assert refused.json()["detail"] == "User not found."


async def test_a_member_cannot_share_an_email_with_another_user_of_the_org(api, session_factory):
    org_b = await create_org(session_factory)
    await create_user(session_factory, org_b, email="Ada@Example.com")
    candidate = await create_user(session_factory, api.org_id, email="ada@example.com")

    async with session_factory() as db:
        with pytest.raises(MembershipError) as clash:
            await add_member(db, org_b, candidate)
    assert clash.value.code == "email-taken"
    assert await memberships_of(session_factory, org_b) == 0

    assert await add(session_factory, org_b, api.user_id) is True
    assert await add(session_factory, org_b, api.user_id) is False
    assert await add(session_factory, api.org_id, api.user_id) is False
    assert await memberships_of(session_factory, org_b) == 1

    async with session_factory() as db:
        with pytest.raises(MembershipError) as home:
            await remove_member(db, api.org_id, api.user_id)
    assert home.value.code == "home-org"


async def test_purging_a_member_keeps_their_account(api, session_factory):
    org_b = await create_org(session_factory)
    await add(session_factory, org_b, api.user_id)
    home_session, b_session = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as db:
        db.add(Session(id=home_session, org_id=api.org_id, user_id=api.user_id, agent_id="agent-x", config={}))
        db.add(Session(id=b_session, org_id=org_b, user_id=api.user_id, agent_id="agent-b", config={}))
        await db.commit()

    async with session_factory() as db:
        await purge_user_account(db, org_id=org_b, user_id=api.user_id)
        await db.commit()

    async with session_factory() as db:
        assert await db.get(User, api.user_id) is not None
        assert (await db.get(Session, home_session)).user_id == api.user_id
        assert (await db.get(Session, b_session)).user_id is None
    assert await memberships_of(session_factory, org_b) == 0
    assert (await me(api, token_for(org_b, api.user_id))).status_code == 401
    assert (await me(api, api.token)).json()["organisations"] == [org(api.org_id)]


async def test_a_home_account_is_not_purged_while_it_belongs_to_other_organisations(api, session_factory):
    org_b = await create_org(session_factory)
    await add(session_factory, org_b, api.user_id)

    async with session_factory() as db:
        with pytest.raises(MembershipsRemain) as refused:
            await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
    assert [o.id for o in refused.value.orgs] == [org_b]
    assert f"test-org-{org_b} ({org_b})" in str(refused.value)
    async with session_factory() as db:
        assert await db.get(User, api.user_id) is not None

    async with session_factory() as db:
        await purge_user_account(db, org_id=org_b, user_id=api.user_id)
        await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
        await db.commit()
    async with session_factory() as db:
        assert await db.get(User, api.user_id) is None


async def test_the_cli_adds_lists_and_removes_a_member(api, session_factory, pg_url, monkeypatch, capsys, tmp_path):
    org_b = await create_org(session_factory)
    resident = await create_user(session_factory, org_b, email="zed@example.com")
    monkeypatch.setenv("SUROGATES_DB_URL", pg_url)
    monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "absent.yaml"))

    async def run(*argv: str) -> tuple[int, str, str]:
        monkeypatch.setattr(sys, "argv", ["surogates", "org-members", *argv])
        try:
            # The command runs its own event loop, as from a shell.
            await asyncio.to_thread(cli_main)
            code = 0
        except SystemExit as exited:
            code = exited.code
        printed = capsys.readouterr()
        return code, printed.out, printed.err

    member = ("--org", str(org_b), "--user", str(api.user_id))
    assert await run("add", *member) == (0, "added\n", "")
    assert await run("add", *member) == (0, "already a member\n", "")
    code, listed, _ = await run("list", "--org", str(org_b))
    assert code == 0
    assert listed.splitlines() == [
        f"{resident} zed@example.com home",
        f"{api.user_id} user-{api.user_id}@test.com member",
    ]
    assert await run("remove", *member) == (0, "removed\n", "")
    assert await run("remove", *member) == (0, "not a member\n", "")

    code, printed, error = await run("remove", "--org", str(api.org_id), "--user", str(api.user_id))
    assert (code, printed) == (1, "")
    assert "home organisation" in error
    assert "@" not in error
