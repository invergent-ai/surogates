"""When the user last signed in, carried by every token, and the computers it lets them add."""

from __future__ import annotations

import time

import pytest
from jose import jwt as jose_jwt

from surogates.tenant.auth.jwt import create_access_token, create_refresh_token

from .test_devices import add_user, api, register  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def claims(token: str) -> dict:
    return jose_jwt.get_unverified_claims(token)


async def test_a_sign_in_stamps_its_time_on_both_tokens(api, session_factory):
    from .conftest import create_user

    await create_user(session_factory, api.org_id, email="ada@example.com", password="testpass123")
    before = int(time.time())
    response = await api.client.post("/v1/auth/login", json={"email": "ada@example.com", "password": "testpass123"})
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert before <= claims(tokens["access_token"])["auth_time"] <= int(time.time())
    assert claims(tokens["refresh_token"])["auth_time"] == claims(tokens["access_token"])["auth_time"]


async def test_a_refresh_keeps_the_sign_in_time_not_its_own(api):
    signed_in = int(time.time()) - 3600
    refresh = create_refresh_token(api.org_id, api.user_id, auth_time=signed_in)
    response = await api.client.post("/v1/auth/refresh", json={"refresh_token": refresh})
    assert response.status_code == 200, response.text
    access = claims(response.json()["access_token"])
    assert access["auth_time"] == signed_in
    assert access["iat"] > signed_in


async def test_a_refresh_token_with_no_sign_in_time_gives_an_access_token_with_none(api):
    response = await api.client.post(
        "/v1/auth/refresh", json={"refresh_token": create_refresh_token(api.org_id, api.user_id)},
    )
    assert response.status_code == 200, response.text
    assert "auth_time" not in claims(response.json()["access_token"])


@pytest.mark.parametrize("auth_time", [None, "eleven minutes ago"])
async def test_adding_a_computer_needs_a_sign_in_from_the_last_ten_minutes(api, auth_time):
    stale = None if auth_time is None else int(time.time()) - 11 * 60
    token = create_access_token(api.org_id, api.user_id, {"sessions:read"}, auth_time=stale)
    response = await api.client.post("/v1/devices", json={"name": "ThinkPad"}, headers=api.auth(token))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "recent_sign_in_required"
    assert (await api.client.get("/v1/devices", headers=api.auth())).json() == []


async def test_restoring_a_computer_needs_a_recent_sign_in_and_revoking_one_does_not(api):
    issued = await register(api)
    stale = create_access_token(api.org_id, api.user_id, {"sessions:read"}, auth_time=int(time.time()) - 11 * 60)
    refused = await api.client.post(f"/v1/devices/{issued['id']}/reauthorize", headers=api.auth(stale))
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "recent_sign_in_required"
    revoked = await api.client.delete(f"/v1/devices/{issued['id']}", headers=api.auth(stale))
    assert revoked.status_code == 204
