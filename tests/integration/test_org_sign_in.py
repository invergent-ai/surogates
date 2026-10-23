"""Signing in by organisation, and the members of an organisation on its agents' sign-in."""

from __future__ import annotations

import time
import uuid
from uuid import UUID

import pytest
import pytest_asyncio
from jose import jwt as jose_jwt
from sqlalchemy import func, select

from surogates.api.routes import auth as auth_routes
from surogates.db.models import AgentUser, User
from surogates.runtime import FirebaseConfig
from surogates.runtime.platform_client import PlatformAuthError
from surogates.tenant.memberships import add_member

from .conftest import create_org, create_user
from .test_devices import AGENT_ID, api  # noqa: F401  (fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")

PASSWORD = "testpass123"
NO_ACCOUNT = "You have no account in this organisation. Ask to be invited."
FIREBASE_UNAVAILABLE = "Firebase auth is unavailable. Try again shortly."
EMAIL_TAKEN = "That email already belongs to an account in this organisation. Sign in with its existing method."


def claims(token: str) -> dict:
    return jose_jwt.get_unverified_claims(token)


def firebase_of(key: str, firebase_project_id: str) -> FirebaseConfig:
    return FirebaseConfig(
        project_id=key,
        firebase_project_id=firebase_project_id,
        api_key="public-key",
        auth_domain=f"{firebase_project_id}.firebaseapp.com",
        enabled_providers=("google", "password"),
    )


class FakeFirebaseCache:
    """The per-project Firebase configs, keyed as the runtime's cache is."""

    def __init__(self, entries: dict[str, FirebaseConfig | Exception]) -> None:
        self._entries = entries

    async def get(self, key: str) -> FirebaseConfig:
        entry = self._entries.get(key)
        if entry is None:
            raise LookupError(key)
        if isinstance(entry, Exception):
            raise entry
        return entry


@pytest.fixture
def org_b_firebase_project() -> str:
    return f"org-b-{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(loop_scope="session")
async def org_b(api, session_factory, monkeypatch, org_b_firebase_project) -> UUID:
    """A second organisation, with its own Firebase project; the agent's project has one too."""
    org_id = await create_org(session_factory)
    monkeypatch.setattr(api.app.state, "firebase_config_cache", FakeFirebaseCache({
        str(org_id): firebase_of(str(org_id), org_b_firebase_project),
        "test-project": firebase_of("test-project", "agent-firebase"),
    }), raising=False)
    return org_id


@pytest.fixture
def firebase_sign_ins(monkeypatch) -> list[str]:
    """Each Firebase ID token verifies as the claims it names; the projects it is checked against are recorded."""
    checked_against: list[str] = []

    async def verify(token: str, firebase_project_id: str) -> dict:
        checked_against.append(firebase_project_id)
        subject, email = token.split("|")
        return {
            "sub": subject,
            "email": email,
            "email_verified": True,
            "auth_time": int(time.time()) - 60,
            "firebase": {"sign_in_provider": "password"},
        }

    monkeypatch.setattr(auth_routes, "verify_firebase_id_token", verify)
    return checked_against


async def add(session_factory, org_id: UUID, user_id: UUID) -> None:
    async with session_factory() as db:
        assert await add_member(db, org_id, user_id) is True
        await db.commit()


async def firebase_account(
    session_factory, org_id: UUID, email: str, firebase_project: str, external_id: str | None = None,
) -> UUID:
    """An account of *org_id* that signs in with *firebase_project*; with no external id, it waits for its first sign-in."""
    user_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(User(
            id=user_id, org_id=org_id, email=email, display_name=email.split("@")[0],
            auth_provider=f"firebase:{firebase_project}", external_id=external_id,
        ))
        await db.commit()
    return user_id


async def email_of(session_factory, user_id: UUID) -> str:
    async with session_factory() as db:
        return await db.scalar(select(User.email).where(User.id == user_id))


async def count_users(session_factory, email: str | None = None) -> int:
    query = select(func.count()).select_from(User)
    if email is not None:
        query = query.where(func.lower(User.email) == email.lower())
    async with session_factory() as db:
        return await db.scalar(query)


async def test_a_member_signs_in_by_organisation_with_their_home_password(api, session_factory, org_b):
    await add(session_factory, org_b, api.user_id)
    email = await email_of(session_factory, api.user_id)

    signed_in = await api.client.post(f"/v1/auth/orgs/{org_b}/login", json={"email": email, "password": PASSWORD})

    assert signed_in.status_code == 200, signed_in.text
    tokens = signed_in.json()
    assert tokens["token_type"] == "bearer"
    assert claims(tokens["access_token"])["org_id"] == str(org_b)
    assert claims(tokens["refresh_token"])["org_id"] == str(org_b)
    me = await api.client.get("/v1/auth/me", headers=api.auth(tokens["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["org_id"] == str(org_b)
    assert [o["id"] for o in me.json()["organisations"]] == [str(api.org_id), str(org_b)]

    wrong = await api.client.post(f"/v1/auth/orgs/{org_b}/login", json={"email": email, "password": "not-it"})
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "Invalid credentials."}

    # A user with no password says no more than a wrong password does.
    await create_user(session_factory, org_b, email="no-password@example.com")
    no_password = await api.client.post(
        f"/v1/auth/orgs/{org_b}/login", json={"email": "no-password@example.com", "password": PASSWORD},
    )
    assert no_password.status_code == 401
    assert no_password.json() == {"detail": "Invalid credentials."}

    unknown = await api.client.post(f"/v1/auth/orgs/{uuid.uuid4()}/login", json={"email": email, "password": PASSWORD})
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Organisation not found."}


async def test_the_org_config_names_the_organisation_and_its_firebase(api, org_b, org_b_firebase_project, monkeypatch):
    config = await api.client.get(f"/v1/auth/orgs/{org_b}/config")

    assert config.status_code == 200, config.text
    assert config.json() == {
        "org_id": str(org_b),
        "org_name": f"test-org-{org_b}",
        "firebase": {
            "api_key": "public-key",
            "auth_domain": f"{org_b_firebase_project}.firebaseapp.com",
            "project_id": org_b_firebase_project,
            "app_id": None,
            "messaging_sender_id": None,
            "measurement_id": None,
            "enabled_providers": ["google", "password"],
        },
    }

    without_firebase = await api.client.get(f"/v1/auth/orgs/{api.org_id}/config")
    assert without_firebase.status_code == 200, without_firebase.text
    assert without_firebase.json() == {"org_id": str(api.org_id), "org_name": f"test-org-{api.org_id}", "firebase": None}

    unknown = await api.client.get(f"/v1/auth/orgs/{uuid.uuid4()}/config")
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Organisation not found."}

    # The platform failing to answer is no "this organisation has no Firebase".
    monkeypatch.setattr(api.app.state, "firebase_config_cache", FakeFirebaseCache({
        str(org_b): PlatformAuthError("rejected"),
    }))
    unavailable = await api.client.get(f"/v1/auth/orgs/{org_b}/config")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": FIREBASE_UNAVAILABLE}


async def test_the_org_firebase_exchange_never_registers_a_user(
    api, session_factory, org_b, org_b_firebase_project, firebase_sign_ins,
):
    users_before = await count_users(session_factory)

    stranger = await api.client.post(
        f"/v1/auth/orgs/{org_b}/firebase/exchange", json={"id_token": "uid-stranger|stranger@example.com"},
    )

    assert stranger.status_code == 403
    assert stranger.json() == {"detail": NO_ACCOUNT}
    assert firebase_sign_ins == [org_b_firebase_project]
    assert await count_users(session_factory) == users_before
    assert await count_users(session_factory, "stranger@example.com") == 0

    # Invited: an account of ORG_B waiting for its first Firebase sign-in.
    invited = await firebase_account(session_factory, org_b, "invited@example.com", org_b_firebase_project)

    linked = await api.client.post(
        f"/v1/auth/orgs/{org_b}/firebase/exchange", json={"id_token": "uid-invited|Invited@Example.com"},
    )

    assert linked.status_code == 200, linked.text
    access = claims(linked.json()["access_token"])
    assert access["org_id"] == str(org_b)
    assert access["user_id"] == str(invited)
    # The Firebase sign-in's time, not the exchange's.
    assert access["auth_time"] < int(time.time()) - 30
    assert claims(linked.json()["refresh_token"])["auth_time"] == access["auth_time"]
    async with session_factory() as db:
        user = await db.get(User, invited)
    assert user.external_id == "uid-invited"
    assert user.sign_in_provider == "password"
    assert await count_users(session_factory) == users_before + 1

    again = await api.client.post(
        f"/v1/auth/orgs/{org_b}/firebase/exchange", json={"id_token": "uid-invited|invited@example.com"},
    )
    assert again.status_code == 200, again.text
    assert claims(again.json()["access_token"])["user_id"] == str(invited)

    unknown = await api.client.post(
        f"/v1/auth/orgs/{uuid.uuid4()}/firebase/exchange", json={"id_token": "uid-invited|invited@example.com"},
    )
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "Organisation not found."}
    no_firebase = await api.client.post(
        f"/v1/auth/orgs/{api.org_id}/firebase/exchange", json={"id_token": "uid-invited|invited@example.com"},
    )
    assert no_firebase.status_code == 404
    assert no_firebase.json() == {"detail": "Firebase auth is not configured."}


async def test_the_org_firebase_exchange_never_relinks_a_member_of_another_organisation(
    api, session_factory, org_b, org_b_firebase_project, firebase_sign_ins,
):
    # Members whose home is the api organisation: one waiting for a Firebase sign-in, one with a password.
    waiting = await firebase_account(session_factory, api.org_id, "waiting@example.com", org_b_firebase_project)
    await add(session_factory, org_b, waiting)
    with_password = await create_user(session_factory, api.org_id, email="local@example.com", password=PASSWORD)
    await add(session_factory, org_b, with_password)

    for member, email in ((waiting, "waiting@example.com"), (with_password, "local@example.com")):
        async with session_factory() as db:
            before = await db.get(User, member)
            identity = (before.auth_provider, before.external_id)

        refused = await api.client.post(
            f"/v1/auth/orgs/{org_b}/firebase/exchange", json={"id_token": f"uid-{uuid.uuid4().hex}|{email}"},
        )

        assert refused.status_code == 403, refused.text
        assert refused.json() == {"detail": NO_ACCOUNT}
        async with session_factory() as db:
            after = await db.get(User, member)
        assert (after.auth_provider, after.external_id) == identity

    # A member already signed in to the same Firebase project is found by that identity.
    shared = await firebase_account(
        session_factory, api.org_id, "shared@example.com", org_b_firebase_project, external_id="uid-shared",
    )
    await add(session_factory, org_b, shared)
    found = await api.client.post(
        f"/v1/auth/orgs/{org_b}/firebase/exchange", json={"id_token": "uid-shared|shared@example.com"},
    )
    assert found.status_code == 200, found.text
    assert claims(found.json()["access_token"])["user_id"] == str(shared)
    assert claims(found.json()["access_token"])["org_id"] == str(org_b)


async def test_the_org_sign_in_ignores_any_agent_the_request_names(api, session_factory, org_b):
    await add(session_factory, org_b, api.user_id)
    email = await email_of(session_factory, api.user_id)
    credentials = {"email": email, "password": PASSWORD}

    by_query = await api.client.post(f"/v1/auth/orgs/{org_b}/login", params={"agent_id": AGENT_ID}, json=credentials)
    by_host = await api.client.post(
        f"/v1/auth/orgs/{org_b}/login", headers={"Host": f"{AGENT_ID}.agents.example.com"}, json=credentials,
    )

    for signed_in in (by_query, by_host):
        assert signed_in.status_code == 200, signed_in.text
        assert claims(signed_in.json()["access_token"])["org_id"] == str(org_b)
    async with session_factory() as db:
        enrolled = await db.scalar(select(func.count()).select_from(AgentUser).where(AgentUser.user_id == api.user_id))
    assert enrolled == 0


async def test_an_agent_login_admits_a_member_of_its_organisation(api, session_factory, org_b):
    member = await create_user(session_factory, org_b, email="ada@example.com", password=PASSWORD)
    await add(session_factory, api.org_id, member)

    signed_in = await api.client.post("/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD})

    assert signed_in.status_code == 200, signed_in.text
    access = claims(signed_in.json()["access_token"])
    assert access["org_id"] == str(api.org_id)
    assert access["user_id"] == str(member)
    me = await api.client.get("/v1/auth/me", headers=api.auth(signed_in.json()["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["org_id"] == str(api.org_id)


async def test_an_agent_sign_up_never_duplicates_a_members_email(api, session_factory, org_b, firebase_sign_ins):
    member = await create_user(session_factory, org_b, email="grace@example.com", password=PASSWORD)
    await add(session_factory, api.org_id, member)
    users_before = await count_users(session_factory)

    refused = await api.client.post(
        "/v1/auth/firebase/exchange", json={"id_token": "uid-new|Grace@Example.com"},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json() == {"detail": EMAIL_TAKEN}
    assert firebase_sign_ins == ["agent-firebase"]
    assert await count_users(session_factory) == users_before
    assert await count_users(session_factory, "grace@example.com") == 1
    async with session_factory() as db:
        kept = await db.get(User, member)
    assert (kept.auth_provider, kept.external_id) == ("database", None)
