"""A website simulation: the run's key routes from the runtime DB through the
API's own cache, and the visitor's session lands in the run's memory."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from surogates.api.app import build_channel_routing_cache
from surogates.api.routes import website
from surogates.channels.memory_boundary import SIMULATION_BOUNDARY_PREFIX
from surogates.channels.website_keys import generate_publishable_key
from surogates.simulation import api
from surogates.simulation.ids import is_simulation_id, is_simulation_website_key, new_website_key
from tests.api.test_website_multi_session import _ORIGIN, _settings, _Storage, _Store
from tests.test_simulation_api import TOKEN, Settings
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import FakeVault, line, make_org, provision_into


def test_only_a_simulation_key_routes_from_the_runtime():
    key = new_website_key()
    assert is_simulation_website_key(key) and is_simulation_id(key)
    real = generate_publishable_key()
    assert not is_simulation_website_key(real)
    # A real key may happen to start like ours; its length never matches.
    assert not is_simulation_website_key("surg_wk_999r" + real[12:])


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
    client = httpx.AsyncClient(transport=ASGITransport(app=internal), base_url="http://simulation",
                               headers={"Authorization": f"Bearer {TOKEN}"})
    run = (await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "hero-agent", "created_by": "u1",
        "batch_id": str(uuid.uuid4()), "people": [line("Ana")], "channel": "website",
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
        assert config["simulation_run_id"] == run["id"]
        assert config["memory_boundary"].startswith(f"{SIMULATION_BOUNDARY_PREFIX}{run['id']}:visitor:")
        assert ops.asked == []

        await client.post(f"/internal/runs/{run['id']}/end", params=org)
        app.state.channel_routing_cache = build_channel_routing_cache(
            settings=None, platform_client=ops, session_factory=rsf)
        r = await w.post("/v1/website/sessions",
                         headers={"Authorization": f"Bearer {key}", "Origin": _ORIGIN})
        assert r.status_code == 404 and ops.asked == [key]


@pytest.mark.asyncio
async def test_a_simulation_visitor_never_meets_the_paywall():
    from types import SimpleNamespace

    from surogates.api.routes._commerce_turn import authorize_commerce_turn

    session = SimpleNamespace(agent_id="a1", id="s1", config={"simulation_run_id": "r1"})
    # No app state at all: the gate must not even ask ops for the agent's mode.
    assert await authorize_commerce_turn(SimpleNamespace(app=None), session, "Salut") is None
