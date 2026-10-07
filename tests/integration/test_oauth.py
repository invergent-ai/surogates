"""Surogate Desktop's sign-in: the agent's OAuth server for a native app (RFC 8252, RFC 7636)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from jose import jwt as jose_jwt
from sqlalchemy import select

from surogates.api.routes.oauth import code_key
from surogates.db.agent_users import purge_user_account
from surogates.db.models import Device
from surogates.devices.store import DeviceStore
from surogates.runtime import agent_runtime_context_dep, build_agent_runtime_context
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.auth.oauth import OAuthTokens

from .test_devices import AGENT_ID, api, eventually, link_url, linked  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio(loop_scope="session")

CLIENT = "surogate-desktop"
REDIRECT = "http://127.0.0.1:43123/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def request_for(challenge: str, **changes) -> dict:
    return {
        "response_type": "code", "client_id": CLIENT, "redirect_uri": REDIRECT, "state": "s" * 24,
        "code_challenge": challenge, "code_challenge_method": "S256", "decision": "allow", **changes,
    }


async def authorize(api, challenge: str, token: str | None = None, **changes):
    return await api.client.post("/v1/auth/oauth/authorize", json=request_for(challenge, **changes), headers=api.auth(token))


async def code_for(api, challenge: str) -> str:
    response = await authorize(api, challenge)
    assert response.status_code == 200, response.text
    sent = urlsplit(response.json()["redirect_to"])
    assert f"{sent.scheme}://{sent.netloc}{sent.path}" == REDIRECT
    query = parse_qs(sent.query)
    assert query["state"] == ["s" * 24]
    return query["code"][0]


async def exchange(api, code: str, verifier: str, **changes):
    form = {"grant_type": "authorization_code", "client_id": CLIENT, "code": code, "redirect_uri": REDIRECT,
            "code_verifier": verifier, **changes}
    return await api.client.post("/v1/auth/oauth/token", data=form)


async def refresh(api, token: str, **changes):
    form = {"grant_type": "refresh_token", "client_id": CLIENT, "refresh_token": token, **changes}
    return await api.client.post("/v1/auth/oauth/token", data=form)


async def signed_in(api) -> dict:
    verifier, challenge = pkce()
    response = await exchange(api, await code_for(api, challenge), verifier)
    assert response.status_code == 200, response.text
    return response.json()


def claims(token: str) -> dict:
    return jose_jwt.get_unverified_claims(token)


async def test_the_desktop_signs_in_with_the_code_and_its_verifier_and_can_add_this_computer(api):
    signed_in_at = claims(api.token)["auth_time"]
    verifier, challenge = pkce()
    response = await exchange(api, await code_for(api, challenge), verifier)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 1800
    assert tokens["refresh_token"].startswith("surg_rt_")
    # The browser's sign-in, not the exchange, is when the user signed in.
    assert tokens["auth_time"] == signed_in_at
    access = claims(tokens["access_token"])
    assert (access["auth_time"], access["client_id"], access["user_id"]) == (signed_in_at, CLIENT, str(api.user_id))
    added = await api.client.post("/v1/devices", json={"name": "ThinkPad"}, headers=api.auth(tokens["access_token"]))
    assert added.status_code == 201, added.text


async def test_a_code_is_spent_by_any_attempt_to_use_it(api):
    verifier, challenge = pkce()
    code = await code_for(api, challenge)
    assert (await exchange(api, code, pkce()[0])).json() == {"error": "invalid_grant"}
    # The right verifier after one wrong try, and then a replay: both refused.
    assert (await exchange(api, code, verifier)).json() == {"error": "invalid_grant"}
    assert (await exchange(api, code, verifier)).status_code == 400


async def test_a_code_lasts_sixty_seconds(api, redis_client):
    code = await code_for(api, pkce()[1])
    assert 0 < await redis_client.ttl(code_key(code)) <= 60


@pytest.mark.parametrize(("changes", "error"), [
    ({"redirect_uri": "http://127.0.0.1:43124/callback"}, "invalid_grant"),
    ({"client_id": "another-client"}, "invalid_client"),
    ({"code_verifier": "short"}, "invalid_grant"),
])
async def test_the_exchange_must_match_the_request(api, changes, error):
    verifier, challenge = pkce()
    code = await code_for(api, challenge)
    response = await exchange(api, code, verifier, **changes)
    assert (response.status_code, response.json()) == (400, {"error": error})
    # Whatever was wrong, the attempt spent the code.
    assert (await exchange(api, code, verifier)).json() == {"error": "invalid_grant"}


async def test_a_code_works_only_at_the_agent_that_issued_it(api):
    verifier, challenge = pkce()
    code = await code_for(api, challenge)
    own = api.app.dependency_overrides[agent_runtime_context_dep]
    api.app.dependency_overrides[agent_runtime_context_dep] = lambda: build_agent_runtime_context({
        "agent_id": "another-agent", "org_id": str(api.org_id), "project_id": "test-project",
        "enabled": True, "version": 1, "storage_key_prefix": "",
    })
    try:
        assert (await exchange(api, code, verifier)).json() == {"error": "invalid_grant"}
    finally:
        api.app.dependency_overrides[agent_runtime_context_dep] = own


@pytest.mark.parametrize(("changes", "status"), [
    ({"client_id": "another-client"}, 400),
    ({"redirect_uri": "http://localhost:43123/callback"}, 400),
    ({"redirect_uri": "https://127.0.0.1:43123/callback"}, 400),
    ({"redirect_uri": "http://127.0.0.1:80/callback"}, 400),
    ({"redirect_uri": "http://127.0.0.1:08080/callback"}, 400),
    ({"redirect_uri": "http://127.0.0.1:43123/elsewhere"}, 400),
    ({"redirect_uri": "http://127.0.0.1:43123/callback?next=x"}, 400),
    ({"redirect_uri": "http://127.0.0.1:43123/callback\n"}, 400),
    ({"state": "s" * 24 + "\n"}, 400),
    ({"code_challenge": "too-short"}, 400),
    ({"code_challenge_method": "plain"}, 422),
    ({"state": "short"}, 400),
    ({"response_type": "token"}, 422),
])
async def test_a_request_the_desktop_would_not_make_gets_no_code(api, changes, status):
    response = await authorize(api, pkce()[1], **changes)
    assert response.status_code == status
    assert "redirect_to" not in response.json()


async def test_declining_sends_the_browser_back_with_no_code(api):
    response = await authorize(api, pkce()[1], decision="deny")
    assert response.status_code == 200, response.text
    sent = urlsplit(response.json()["redirect_to"])
    assert parse_qs(sent.query) == {"error": ["access_denied"], "state": ["s" * 24]}


@pytest.mark.parametrize("auth_time", [None, "eleven minutes ago"])
async def test_allowing_needs_a_sign_in_from_the_last_ten_minutes(api, auth_time):
    stale = None if auth_time is None else int(time.time()) - 11 * 60
    token = create_access_token(api.org_id, api.user_id, {"sessions:read"}, auth_time=stale)
    response = await authorize(api, pkce()[1], token=token)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "recent_sign_in_required"


async def test_a_refresh_spends_its_token_and_keeps_the_sign_in_time(api):
    tokens = await signed_in(api)
    response = await refresh(api, tokens["refresh_token"])
    assert response.status_code == 200, response.text
    renewed = response.json()
    assert renewed["refresh_token"] != tokens["refresh_token"]
    assert renewed["auth_time"] == tokens["auth_time"]
    assert claims(renewed["access_token"])["auth_time"] == tokens["auth_time"]


async def test_a_spent_refresh_token_used_again_ends_the_whole_sign_in(api):
    tokens = await signed_in(api)
    renewed = (await refresh(api, tokens["refresh_token"])).json()
    # Someone else holds the spent token: neither copy works from now on.
    assert (await refresh(api, tokens["refresh_token"])).json() == {"error": "invalid_grant"}
    assert (await refresh(api, renewed["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_a_refresh_token_works_only_for_its_client(api):
    tokens = await signed_in(api)
    assert (await refresh(api, tokens["refresh_token"], client_id="another-client")).status_code == 400
    assert (await refresh(api, tokens["refresh_token"])).status_code == 200


async def test_revoking_ends_the_sign_in_and_an_unknown_token_is_no_error(api):
    tokens = await signed_in(api)
    revoked = await api.client.post("/v1/auth/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": CLIENT})
    assert revoked.status_code == 200
    assert (await refresh(api, tokens["refresh_token"])).json() == {"error": "invalid_grant"}
    unknown = await api.client.post("/v1/auth/oauth/revoke", data={"token": "surg_rt_unknown", "client_id": CLIENT})
    assert unknown.status_code == 200


async def test_only_the_desktop_revokes_its_sign_in(api):
    tokens = await signed_in(api)
    revoked = await api.client.post("/v1/auth/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": "another-client"})
    assert (revoked.status_code, revoked.json()) == (400, {"error": "invalid_client"})
    assert (await refresh(api, tokens["refresh_token"])).status_code == 200


@pytest.mark.parametrize(("path", "form"), [
    ("/v1/auth/oauth/token", {"client_id": CLIENT, "refresh_token": "surg_rt_x"}),
    ("/v1/auth/oauth/token", {"grant_type": "refresh_token", "refresh_token": "surg_rt_x"}),
    ("/v1/auth/oauth/revoke", {"client_id": CLIENT}),
    ("/v1/auth/oauth/revoke", {"token": "surg_rt_x"}),
])
async def test_a_request_missing_a_field_is_an_invalid_request(api, path, form):
    response = await api.client.post(path, data=form)
    assert (response.status_code, response.json()) == (400, {"error": "invalid_request"})


async def test_a_token_request_sent_as_json_is_an_invalid_request(api):
    response = await api.client.post("/v1/auth/oauth/token", json={"grant_type": "refresh_token", "client_id": CLIENT})
    assert (response.status_code, response.json()) == (400, {"error": "invalid_request"})


async def test_a_deleted_account_signs_its_desktop_out(api, session_factory):
    tokens = await signed_in(api)
    async with session_factory() as db:
        await purge_user_account(db, org_id=api.org_id, user_id=api.user_id)
        await db.commit()
    assert (await refresh(api, tokens["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_the_desktop_gives_its_window_a_session_of_its_own_once(api):
    tokens = await signed_in(api)
    minted = await api.client.post("/v1/auth/oauth/web-code", headers=api.auth(tokens["access_token"]))
    assert minted.status_code == 200, minted.text
    code = minted.json()["code"]
    session = await api.client.post("/v1/auth/oauth/web-session", json={"code": code})
    assert session.status_code == 200, session.text
    assert minted.headers["cache-control"] == session.headers["cache-control"] == "no-store"
    web = session.json()
    assert claims(web["access_token"])["auth_time"] == tokens["auth_time"]
    assert claims(web["refresh_token"])["type"] == "refresh"
    assert "client_id" not in claims(web["access_token"])
    assert (await api.client.post("/v1/auth/oauth/web-session", json={"code": code})).status_code == 400


async def test_only_the_desktops_own_token_mints_a_window_session(api):
    response = await api.client.post("/v1/auth/oauth/web-code", headers=api.auth())
    assert response.status_code == 403


async def add_computer(api, access_token: str) -> str:
    added = await api.client.post("/v1/devices", json={"name": "ThinkPad"}, headers=api.auth(access_token))
    assert added.status_code == 201, added.text
    return added.json()["id"]


async def test_revoking_the_computer_ends_the_sign_in_that_added_it(api):
    tokens = await signed_in(api)
    device_id = await add_computer(api, tokens["access_token"])
    assert (await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth())).status_code == 204
    assert (await refresh(api, tokens["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_the_computer_revoking_itself_ends_its_sign_in_too(api):
    tokens = await signed_in(api)
    device_id = await add_computer(api, tokens["access_token"])
    # As the link's revoke frame does, at the device's credential generation.
    assert await DeviceStore(api.app.state.session_factory).revoke_by_id(UUID(device_id), 1) is not None
    assert (await refresh(api, tokens["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_the_desktop_taking_back_a_computer_that_never_connected_stays_signed_in(api):
    tokens = await signed_in(api)
    device_id = await add_computer(api, tokens["access_token"])
    # The link checked the computer's token, and then the app never got its welcome.
    await DeviceStore(api.app.state.session_factory).touch(UUID(device_id))
    # As the app does with a computer it added but could not keep.
    removed = await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth(tokens["access_token"]))
    assert removed.status_code == 204
    assert (await refresh(api, tokens["refresh_token"])).status_code == 200
    # Nothing is left of it: each failed try leaves no row behind.
    listed = await api.client.get("/v1/devices", headers=api.auth())
    assert device_id not in [device["id"] for device in listed.json()]


async def welcomed(api, device_id: str) -> bool:
    async with api.app.state.session_factory() as db:
        return await db.scalar(select(Device.connected_at).where(Device.id == UUID(device_id))) is not None


@pytest.mark.parametrize("removed_by", ["its own sign-in, once it connected", "another sign-in of the desktop"])
async def test_any_other_removal_of_the_computer_ends_its_sign_in(api, link_url, removed_by):
    tokens = await signed_in(api)
    added = await api.client.post("/v1/devices", json={"name": "ThinkPad"}, headers=api.auth(tokens["access_token"]))
    device_id = added.json()["id"]
    if removed_by == "another sign-in of the desktop":
        remover = (await signed_in(api))["access_token"]
    else:
        async with linked(link_url, added.json()["token"]):
            await eventually(lambda: welcomed(api, device_id))
        remover = tokens["access_token"]
    assert (await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth(remover))).status_code == 204
    assert (await refresh(api, tokens["refresh_token"])).json() == {"error": "invalid_grant"}


async def test_the_sign_in_that_restores_a_computer_ends_with_it_too(api):
    first = await signed_in(api)
    device_id = await add_computer(api, first["access_token"])
    await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth())
    second = await signed_in(api)
    restored = await api.client.post(f"/v1/devices/{device_id}/reauthorize", headers=api.auth(second["access_token"]))
    assert restored.status_code == 200, restored.text
    await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth())
    assert (await refresh(api, second["refresh_token"])).json() == {"error": "invalid_grant"}


@pytest.mark.parametrize(("days", "status"), [(29, 200), (31, 400)])
async def test_a_sign_in_lasts_thirty_days_from_the_browser_sign_in(api, days, status):
    grant = await OAuthTokens(api.app.state.session_factory).issue(
        org_id=api.org_id, user_id=api.user_id, agent_id=AGENT_ID, client_id=CLIENT,
        auth_time=int(time.time()) - days * 86400,
    )
    assert (await refresh(api, grant.refresh_token)).status_code == status


async def test_the_desktop_cannot_allow_itself_another_sign_in(api):
    tokens = await signed_in(api)
    response = await authorize(api, pkce()[1], token=tokens["access_token"])
    assert response.status_code == 403


async def window_session(api, access_token: str) -> dict:
    minted = await api.client.post("/v1/auth/oauth/web-code", headers=api.auth(access_token))
    assert minted.status_code == 200, minted.text
    session = await api.client.post("/v1/auth/oauth/web-session", json={"code": minted.json()["code"]})
    assert session.status_code == 200, session.text
    return session.json()


async def test_the_windows_session_ends_with_the_computer(api):
    tokens = await signed_in(api)
    device_id = await add_computer(api, tokens["access_token"])
    web = await window_session(api, tokens["access_token"])
    renewed = await api.client.post("/v1/auth/refresh", json={"refresh_token": web["refresh_token"]})
    assert renewed.status_code == 200, renewed.text
    # The window's tokens belong to the desktop's sign-in, so they end with it.
    assert claims(renewed.json()["access_token"])["sid"] == claims(tokens["access_token"])["sid"]
    assert (await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth())).status_code == 204
    assert (await api.client.post("/v1/auth/refresh", json={"refresh_token": web["refresh_token"]})).status_code == 401


@pytest.mark.parametrize("ended", ["signed out", "thirty days old"])
async def test_an_ended_sign_in_gives_its_window_no_session(api, ended):
    if ended == "signed out":
        tokens = await signed_in(api)
        await api.client.post("/v1/auth/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": CLIENT})
        access_token = tokens["access_token"]
    else:
        signed_in_at = int(time.time()) - 31 * 86400
        grant = await OAuthTokens(api.app.state.session_factory).issue(
            org_id=api.org_id, user_id=api.user_id, agent_id=AGENT_ID, client_id=CLIENT, auth_time=signed_in_at,
        )
        access_token = create_access_token(
            api.org_id, api.user_id, {"sessions:read"}, auth_time=signed_in_at, client_id=CLIENT, family_id=grant.family_id,
        )
    # The sign-in's access token lives on for minutes, but mints nothing that would outlive it.
    response = await api.client.post("/v1/auth/oauth/web-code", headers=api.auth(access_token))
    assert response.status_code == 403


async def test_the_desktops_window_cannot_allow_another_sign_in_either(api):
    tokens = await signed_in(api)
    web = await window_session(api, tokens["access_token"])
    response = await authorize(api, pkce()[1], token=web["access_token"])
    assert response.status_code == 403


async def test_a_later_sign_in_the_app_binds_to_its_computer_ends_with_it_and_so_does_its_windows_session(api):
    first = await signed_in(api)
    device_id = await add_computer(api, first["access_token"])
    # That sign-in ends, by signing out or after its 30 days; the computer stays, on a token of its own.
    await api.client.post("/v1/auth/oauth/revoke", data={"token": first["refresh_token"], "client_id": CLIENT})
    second = await signed_in(api)
    # As the app does with every later sign-in: it binds it to the computer it keeps.
    listed = (await api.client.get("/v1/devices", headers=api.auth(second["access_token"]))).json()
    assert [device["revoked_at"] for device in listed if device["id"] == device_id] == [None]
    bound = await api.client.post(f"/v1/devices/{device_id}/reauthorize", headers=api.auth(second["access_token"]))
    assert bound.status_code == 200, bound.text
    web = await window_session(api, second["access_token"])
    assert (await api.client.delete(f"/v1/devices/{device_id}", headers=api.auth())).status_code == 204
    assert (await refresh(api, second["refresh_token"])).json() == {"error": "invalid_grant"}
    assert (await api.client.post("/v1/auth/refresh", json={"refresh_token": web["refresh_token"]})).status_code == 401
