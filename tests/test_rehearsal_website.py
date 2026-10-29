"""A website rehearsal: the run's key routes from the runtime DB through the
API's own cache, and the visitor's session lands in the run's memory."""

from __future__ import annotations


import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.api.app import build_channel_routing_cache
from surogates.api.routes import website
from surogates.channels.memory_boundary import REHEARSAL_BOUNDARY_PREFIX
from surogates.channels.website_keys import generate_publishable_key
from surogates.rehearsal import api
from surogates.rehearsal.ids import is_rehearsal_id, is_rehearsal_website_key, new_website_key
from tests.api.test_website_multi_session import _ORIGIN, _settings, _Storage, _Store
from tests.test_rehearsal_api import TOKEN, Settings
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import FakeVault, make_org, provision_into


def test_only_a_rehearsal_key_routes_from_the_runtime():
    key = new_website_key()
    assert is_rehearsal_website_key(key) and is_rehearsal_id(key)
    real = generate_publishable_key()
    assert not is_rehearsal_website_key(real)
    # A real key may happen to start like ours; its length never matches.
    assert not is_rehearsal_website_key("surg_wk_999r" + real[12:])


class _Ops:
    def __init__(self):
        self.asked: list[str] = []

    async def get_channel_routing(self, kind, identifier):
        self.asked.append(identifier)
        return None


@pytest.mark.asyncio
async def test_website_run(rsf):  # noqa: F811
    org_id = await make_org(rsf)
    internal = FastAPI()
    internal.include_router(api.router)
    internal.state.session_factory, internal.state.settings = rsf, Settings
    internal.state.vault, internal.state.provision = FakeVault(), provision_into(rsf)
    internal.state.http = httpx.AsyncClient()
    client = httpx.AsyncClient(transport=ASGITransport(app=internal), base_url="http://rehearsal",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "hero-agent", "created_by": "u1",
        "person_name": "Ana", "channel": "website",
        "routing_config": {"allowed_origins": [_ORIGIN]},
    })).json()
    key, person, org = run["phone_number_id"], run["people"][0], {"org_id": str(org_id)}
    assert person["address"] == "visitor"
    written = await client.post(f"/internal/runs/{run['id']}/people/{person['id']}/messages",
                                params=org, json={"text": "Salut"})
    assert written.status_code == 422

    ops, sessions = _Ops(), _Store()
    app = FastAPI()
    app.include_router(website.router, prefix="/v1")
    app.state.settings, app.state.session_store, app.state.storage = _settings(), sessions, _Storage()
    app.state.channel_routing_cache = build_channel_routing_cache(
        settings=None, platform_client=ops, session_factory=rsf)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://widget") as w:
        r = await w.post("/v1/website/sessions",
                         headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN})
        assert r.status_code == 201, r.text
        config = sessions.created[0]["config"]
        assert config["rehearsal_run_id"] == run["id"]
        assert config["memory_boundary"].startswith(f"{REHEARSAL_BOUNDARY_PREFIX}{run['id']}:visitor:")
        assert ops.asked == []

        await client.post(f"/internal/runs/{run['id']}/end", params=org)
        app.state.channel_routing_cache = build_channel_routing_cache(
            settings=None, platform_client=ops, session_factory=rsf)
        r = await w.post("/v1/website/sessions",
                         headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN})
        assert r.status_code == 404 and ops.asked == [key]
