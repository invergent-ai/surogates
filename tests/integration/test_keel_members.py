"""Keel invites staff into an organisation through ``keel-link``, and a new password user sets their password from a link.

An invitation finds the organisation's user with the email, joins the one
user of another organisation who has it, or creates the account.  A new
password account has no password: the invitation answers a token, and the
runtime's public set-password routes take it once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from jose import jwt as jose_jwt
from sqlalchemy import func, select, text

from surogates.audit import AuditStore, AuditType
from surogates.db.models import AuditLog, OrgMember, User
from surogates.runtime.platform_client import PlatformAuthError
from surogates.tenant.auth.jwt import create_access_token, create_refresh_token
from surogates.tenant.invitations import set_password_token

from .conftest import create_org, create_user
from .test_devices import api  # noqa: F401  (fixture)
from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    LINK_KEY,
    code_of,
    link,
    link_app,
    link_headers,
    org,
)
from .test_org_sign_in import FakeFirebaseCache, firebase_of, firebase_sign_ins, unique_uid  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOT_VALID = {"detail": "This link is not valid or has expired."}
PASSWORD = "correct-horse-battery"
OTHER_PASSWORD = "another-horse-battery"


def bank_email(name: str) -> str:
    """An email no other test uses: the database is shared within a worker, and an invitation looks everywhere."""
    return f"{name}-{uuid.uuid4().hex[:8]}@bank.test"


def secret() -> str:
    """The runtime's signing secret, as the fixtures set it."""
    return os.environ["SUROGATES_JWT_SECRET"]


async def invite(link, org_id, email: str, display_name: str = "Ana Pop", *, key: str | None = LINK_KEY):
    return await link.post(
        f"/v1/keel/orgs/{org_id}/members",
        json={"email": email, "display_name": display_name},
        headers=link_headers(key),
    )


async def check(api, org_id, token: str):
    return await api.client.post(f"/v1/auth/orgs/{org_id}/set-password/check", json={"token": token})


async def set_password(api, org_id, token: str, password: str = PASSWORD):
    return await api.client.post(
        f"/v1/auth/orgs/{org_id}/set-password", json={"token": token, "password": password},
    )


async def sign_in(api, org_id, email: str, password: str):
    return await api.client.post(f"/v1/auth/orgs/{org_id}/login", json={"email": email, "password": password})


async def user_of(session_factory, user_id) -> User:
    async with session_factory() as db:
        return await db.get(User, UUID(str(user_id)))


async def member_orgs(session_factory, user_id: UUID) -> set[UUID]:
    """The organisations *user_id* is a member of, beyond their home."""
    async with session_factory() as db:
        return set((await db.scalars(select(OrgMember.org_id).where(OrgMember.user_id == user_id))).all())


async def users_with(session_factory, email: str) -> list[User]:
    async with session_factory() as db:
        return list((await db.scalars(select(User).where(func.lower(User.email) == email.lower()))).all())


async def firebase_account(session_factory, org_id: UUID, email: str, firebase_project: str) -> UUID:
    """A Firebase account of *org_id*, signed in once to *firebase_project*."""
    user_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(User(
            id=user_id, org_id=org_id, email=email, display_name="Firebase Person",
            auth_provider=f"firebase:{firebase_project}", external_id=unique_uid("fb"),
        ))
        await db.commit()
    return user_id


def changed_byte(token: str) -> str:
    """*token* with one character of its signature changed."""
    at = len(token) - 10
    return token[:at] + ("A" if token[at] != "A" else "B") + token[at + 1:]


async def test_a_new_email_creates_a_password_user_with_a_link(
    link, api, org, session_factory, monkeypatch, caplog,
):
    monkeypatch.setattr(api.app.state, "audit_store", AuditStore(session_factory), raising=False)
    caplog.set_level(logging.DEBUG)
    before = datetime.now(UTC)

    invited = await invite(link, org, "ana@bank.test", "Ana Pop")

    assert invited.status_code == 200, invited.text
    answer = invited.json()
    assert invited.headers["cache-control"] == "no-store"
    assert {k: answer[k] for k in ("created", "joined", "sign_in")} == {
        "created": True, "joined": False, "sign_in": "password",
    }
    token = answer["set_password_token"]
    assert isinstance(token, str) and token
    expires_at = datetime.fromisoformat(answer["expires_at"])
    assert before + timedelta(days=7) - timedelta(seconds=1) <= expires_at <= datetime.now(UTC) + timedelta(days=7)
    user = await user_of(session_factory, answer["user_id"])
    assert (user.org_id, user.email, user.display_name, user.auth_provider) == (org, "ana@bank.test", "Ana Pop", "database")
    assert user.password_hash is None and user.external_id is None

    checked = await check(api, org, token)
    assert checked.status_code == 200, checked.text
    assert checked.json() == {"email": "ana@bank.test", "display_name": "Ana Pop"}

    tokens = await set_password(api, org, token)
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["token_type"] == "bearer"
    me = await api.client.get("/v1/auth/me", headers=api.auth(tokens.json()["access_token"]))
    assert me.status_code == 200, me.text
    assert (me.json()["org_id"], me.json()["email"]) == (str(org), "ana@bank.test")
    async with session_factory() as db:
        logins = (await db.scalars(
            select(AuditLog).where(AuditLog.user_id == user.id, AuditLog.type == AuditType.AUTH_LOGIN.value)
        )).all()
    assert [(row.org_id, row.agent_id) for row in logins] == [(org, None)]

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in logged
    assert "ana@bank.test" not in logged


async def test_a_set_password_link_works_once(link, api, org, session_factory):
    email = bank_email("once")
    token = (await invite(link, org, email)).json()["set_password_token"]

    first = await set_password(api, org, token, PASSWORD)
    assert first.status_code == 200, first.text

    again = await set_password(api, org, token, OTHER_PASSWORD)
    assert again.status_code == 400
    assert again.json() == NOT_VALID
    assert (await check(api, org, token)).json() == NOT_VALID

    assert (await sign_in(api, org, email, PASSWORD)).status_code == 200
    assert (await sign_in(api, org, email, OTHER_PASSWORD)).status_code == 401


async def waiting_on_locks(session_factory, count: int) -> None:
    """Returns once *count* statements wait on a row lock they asked for."""
    for _ in range(500):
        async with session_factory() as db:
            waiting = await db.scalar(text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%FOR UPDATE%'"
            ))
        if waiting >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{count} statements never waited on a lock")


async def test_two_uses_of_one_link_at_once_set_one_password(link, api, org, session_factory):
    email = bank_email("race")
    answer = (await invite(link, org, email)).json()
    token = answer["set_password_token"]

    # The account's row is held until both uses, their link checked and their
    # password hashed, wait on it to write: neither has written before the other reads.
    async with session_factory() as holder:
        await holder.execute(select(User.id).where(User.id == UUID(answer["user_id"])).with_for_update())
        uses = asyncio.gather(
            set_password(api, org, token, PASSWORD), set_password(api, org, token, OTHER_PASSWORD),
        )
        await waiting_on_locks(session_factory, 2)
        await holder.rollback()
    answers = await uses

    assert sorted(a.status_code for a in answers) == [200, 400]
    chosen = PASSWORD if answers[0].status_code == 200 else OTHER_PASSWORD
    refused = OTHER_PASSWORD if chosen == PASSWORD else PASSWORD
    assert (await sign_in(api, org, email, chosen)).status_code == 200
    assert (await sign_in(api, org, email, refused)).status_code == 401


async def test_a_link_of_another_organisation_or_a_lapsed_one_is_refused(link, api, org, session_factory):
    answer = (await invite(link, org, bank_email("lapsed"))).json()
    token = answer["set_password_token"]
    org_b = await create_org(session_factory)
    user = await user_of(session_factory, answer["user_id"])
    lapsed, _ = set_password_token(user, org, secret(), datetime.now(UTC) - timedelta(days=8))
    forged, _ = set_password_token(user, org, "not-the-runtime-secret", datetime.now(UTC))

    for refused, route_org in (
        (token, org_b), (lapsed, org), (changed_byte(token), org), (forged, org), ("not-a-token", org),
    ):
        assert (await check(api, route_org, refused)).json() == NOT_VALID
        answered = await set_password(api, route_org, refused)
        assert answered.status_code == 400
        assert answered.json() == NOT_VALID

    unknown = await set_password(api, uuid.uuid4(), token)
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Organisation not found."}
    assert (await check(api, uuid.uuid4(), token)).status_code == 404

    # None of those set a password: the link still works.
    assert (await user_of(session_factory, answer["user_id"])).password_hash is None
    assert (await set_password(api, org, token)).status_code == 200


async def test_a_set_password_link_is_no_access_token_nor_the_other_way_round(link, api, org):
    answer = (await invite(link, org, bank_email("purpose"))).json()
    token = answer["set_password_token"]

    claims = jose_jwt.get_unverified_claims(token)
    assert claims["purpose"] == "set-password"
    assert "type" not in claims and "permissions" not in claims
    me = await api.client.get("/v1/auth/me", headers=api.auth(token))
    assert me.status_code == 401
    refreshed = await api.client.post("/v1/auth/refresh", json={"refresh_token": token})
    assert refreshed.status_code == 401

    user_id = UUID(answer["user_id"])
    for other in (
        create_access_token(org, user_id, {"sessions:read"}),
        create_refresh_token(org, user_id),
    ):
        assert (await check(api, org, other)).json() == NOT_VALID
        assert (await set_password(api, org, other)).json() == NOT_VALID


async def test_a_password_the_rules_refuse_is_422_and_leaves_the_link(link, api, org):
    token = (await invite(link, org, bank_email("rules"))).json()["set_password_token"]

    short = await set_password(api, org, token, "short")
    assert short.status_code == 422
    assert short.json() == {"detail": "New password must be at least 8 characters."}
    too_long = await set_password(api, org, token, "x" * 73)
    assert too_long.status_code == 422
    assert too_long.json() == {"detail": "New password must be at most 72 bytes."}
    # A lone surrogate, as JSON escapes it.
    not_text = await api.client.post(
        f"/v1/auth/orgs/{org}/set-password",
        content=json.dumps({"token": token, "password": "\ud800" * 8}),
        headers={"Content-Type": "application/json"},
    )
    assert not_text.status_code == 422
    assert not_text.json() == {"detail": "New password must be valid text."}
    # A body without a password is refused by the rules, and the answer does not quote the token.
    missing = await api.client.post(f"/v1/auth/orgs/{org}/set-password", json={"token": token})
    assert missing.status_code == 422
    assert missing.json() == {"detail": "New password must be at least 8 characters."}
    assert token not in missing.text

    assert (await set_password(api, org, token)).status_code == 200


async def test_an_existing_member_is_found_and_someone_elsewhere_joins(link, api, org, session_factory):
    member_email = bank_email("member")
    member = await create_user(session_factory, org, email=member_email, password=PASSWORD)

    found = await invite(link, org, member_email.upper(), "Another Name")

    assert found.status_code == 200, found.text
    assert found.json() == {
        "user_id": str(member), "created": False, "joined": False, "sign_in": "password",
        "set_password_token": None, "expires_at": None,
    }
    assert (await user_of(session_factory, member)).display_name == f"Test User {member}"

    # The one user of ORG_B with the email, written in another case.
    elsewhere_email = bank_email("elsewhere")
    elsewhere = await create_user(session_factory, api.org_id, email=elsewhere_email, password=PASSWORD)
    before = await user_of(session_factory, elsewhere)

    joined = await invite(link, org, elsewhere_email.upper(), "Another Name")

    assert joined.status_code == 200, joined.text
    assert joined.json() == {
        "user_id": str(elsewhere), "created": False, "joined": True, "sign_in": "password",
        "set_password_token": None, "expires_at": None,
    }
    assert await member_orgs(session_factory, elsewhere) == {org}
    after = await user_of(session_factory, elsewhere)
    assert (after.org_id, after.email, after.display_name, after.auth_provider, after.password_hash) == (
        before.org_id, before.email, before.display_name, before.auth_provider, before.password_hash,
    )
    assert (await sign_in(api, org, elsewhere_email, PASSWORD)).status_code == 200

    again = await invite(link, org, elsewhere_email)
    assert (again.json()["joined"], again.json()["created"]) == (False, False)
    assert await member_orgs(session_factory, elsewhere) == {org}


async def test_a_link_never_sets_the_password_of_an_account_whose_home_is_elsewhere(
    link, api, org, session_factory,
):
    email = bank_email("waiting")
    waiting = await create_user(session_factory, api.org_id, email=email)

    joined = await invite(link, org, email)

    assert joined.status_code == 200, joined.text
    assert (joined.json()["joined"], joined.json()["sign_in"]) == (True, "password")
    assert (joined.json()["set_password_token"], joined.json()["expires_at"]) == (None, None)
    found = await invite(link, org, email)
    assert (found.json()["joined"], found.json()["set_password_token"]) == (False, None)

    # Nor does a token naming this organisation for that account.
    user = await user_of(session_factory, waiting)
    token, _ = set_password_token(user, org, secret(), datetime.now(UTC))
    assert (await set_password(api, org, token)).json() == NOT_VALID
    assert (await user_of(session_factory, waiting)).password_hash is None


async def test_an_email_two_users_share_elsewhere_is_ambiguous(link, org, session_factory):
    email = bank_email("shared")
    first = await create_user(session_factory, await create_org(session_factory), email=email)
    second = await create_user(session_factory, await create_org(session_factory), email=email.upper())

    refused = await invite(link, org, email)

    assert refused.status_code == 409
    assert code_of(refused) == "ambiguous-email"
    assert {u.id for u in await users_with(session_factory, email)} == {first, second}
    assert await member_orgs(session_factory, first) == set()
    assert await member_orgs(session_factory, second) == set()


async def test_a_firebase_organisation_creates_a_firebase_user_without_a_link(
    link, link_app, api, session_factory, monkeypatch, firebase_sign_ins,
):
    org_f = await create_org(session_factory)
    project = f"org-f-{uuid.uuid4().hex[:8]}"
    cache = FakeFirebaseCache({str(org_f): firebase_of(str(org_f), project)})
    monkeypatch.setattr(link_app.state, "firebase_config_cache", cache, raising=False)
    monkeypatch.setattr(api.app.state, "firebase_config_cache", cache, raising=False)
    email = bank_email("cleo")

    invited = await invite(link, org_f, f"  {email.upper()} ", "Cleo Ionescu")

    assert invited.status_code == 200, invited.text
    answer = invited.json()
    assert {k: answer[k] for k in ("created", "joined", "sign_in", "set_password_token", "expires_at")} == {
        "created": True, "joined": False, "sign_in": "firebase", "set_password_token": None, "expires_at": None,
    }
    user = await user_of(session_factory, answer["user_id"])
    assert (user.org_id, user.email, user.auth_provider) == (org_f, email, f"firebase:{project}")
    assert user.external_id is None and user.password_hash is None

    uid = unique_uid("cleo")
    linked = await api.client.post(f"/v1/auth/orgs/{org_f}/firebase/exchange", json={"id_token": f"{uid}|{email}"})

    assert linked.status_code == 200, linked.text
    assert jose_jwt.get_unverified_claims(linked.json()["access_token"])["user_id"] == str(user.id)
    assert (await user_of(session_factory, user.id)).external_id == uid
    found = await invite(link, org_f, email)
    assert (found.json()["created"], found.json()["sign_in"]) == (False, "firebase")


async def test_a_firebase_account_joins_only_an_organisation_of_its_project(
    link, link_app, org, session_factory, monkeypatch,
):
    org_f = await create_org(session_factory)
    project = f"org-f-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(link_app.state, "firebase_config_cache", FakeFirebaseCache({
        str(org_f): firebase_of(str(org_f), project),
    }), raising=False)

    # An account of the organisation's own project, whose home is elsewhere, joins it.
    same_email = bank_email("same-project")
    same = await firebase_account(session_factory, await create_org(session_factory), same_email, project)
    joined = await invite(link, org_f, same_email)
    assert joined.status_code == 200, joined.text
    assert joined.json() == {
        "user_id": str(same), "created": False, "joined": True, "sign_in": "firebase",
        "set_password_token": None, "expires_at": None,
    }
    assert await member_orgs(session_factory, same) == {org_f}

    email = bank_email("foreign")
    foreign = await firebase_account(session_factory, await create_org(session_factory), email, "another-project")

    # Into an organisation of another Firebase project, and into one with none.
    for org_id in (org_f, org):
        refused = await invite(link, org_id, email)
        assert refused.status_code == 409
        assert code_of(refused) == "other-firebase-project"
    assert await member_orgs(session_factory, foreign) == set()


async def test_an_unreadable_firebase_config_is_unavailable(link, link_app, session_factory, monkeypatch, caplog):
    org_f = await create_org(session_factory)
    monkeypatch.setattr(link_app.state, "firebase_config_cache", FakeFirebaseCache({
        str(org_f): PlatformAuthError("rejected"),
    }), raising=False)
    email = bank_email("outage")

    with caplog.at_level(logging.WARNING, logger="surogates.api.routes.keel"):
        refused = await invite(link, org_f, email)

    assert refused.status_code == 503
    assert code_of(refused) == "unavailable"
    assert await users_with(session_factory, email) == []
    # The failure is logged by its class alone: its text is the platform's.
    [logged] = [record for record in caplog.records if record.name == "surogates.api.routes.keel"]
    assert str(org_f) in logged.getMessage() and "PlatformAuthError" in logged.getMessage()
    assert "rejected" not in logged.getMessage() and logged.exc_info is None


async def test_the_members_route_needs_the_link_key_and_a_known_org(link, org, session_factory):
    email = bank_email("door")

    for key in (None, "not-the-key"):
        refused = await invite(link, org, email, key=key)
        assert refused.status_code == 401
        assert code_of(refused) == "bad-link-key"
    unknown = await invite(link, uuid.uuid4(), email)
    assert unknown.status_code == 404
    assert code_of(unknown) == "unknown-org"
    for bad in ("not-an-email", "two@at@bank.test", "@bank.test", "ana@", "an a@bank.test"):
        invalid = await invite(link, org, bad)
        assert invalid.status_code == 422
        assert code_of(invalid) == "invalid-request"
    not_an_org = await invite(link, "not-a-uuid", email)
    assert not_an_org.status_code == 422
    assert code_of(not_an_org) == "invalid-request"
    assert await users_with(session_factory, email) == []
