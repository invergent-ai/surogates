"""Same-origin Firebase auth-helper proxy: pinned upstream, helper
namespace only, graceful failures."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from surogates.api.routes import firebase_auth_proxy
from surogates.runtime import AgentRuntimeContext, FirebaseConfig


def _ctx() -> AgentRuntimeContext:
    return AgentRuntimeContext(
        agent_id="a-1",
        org_id="o-1",
        project_id="p-1",
        enabled=True,
        config_version=1,
        storage_key_prefix="p-1/a-1",
    )


def _firebase() -> FirebaseConfig:
    return FirebaseConfig(
        project_id="p-1",
        firebase_project_id="proj-x",
        api_key="AIzaX",
        auth_domain="proj-x.firebaseapp.com",
        enabled_providers=("google",),
    )


class _FakeCache:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        if key not in self._rows:
            raise LookupError(key)
        return self._rows[key]


class _FakeUpstream:
    def __init__(self):
        self.requests = []

    async def request(self, method, url, headers=None, content=None):
        self.requests.append((method, str(url), headers))
        return httpx.Response(
            200,
            content=b"<html>helper</html>",
            headers={"content-type": "text/html", "set-cookie": "x=1"},
        )


def _request(*, firebase=None, upstream=None):
    state = SimpleNamespace()
    if firebase is not None:
        state.firebase_config_cache = _FakeCache({"p-1": firebase})
    if upstream is not None:
        state.firebase_helper_client = upstream
    async def body():
        return b""
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        headers={"host": "ludwig.cloud.surogate.ai", "cookie": "secret"},
        method="GET",
        url=SimpleNamespace(query="apiKey=AIzaX"),
        body=body,
    )


@pytest.mark.asyncio
async def test_proxies_helper_to_the_project_domain():
    upstream = _FakeUpstream()
    request = _request(firebase=_firebase(), upstream=upstream)
    resp = await firebase_auth_proxy.firebase_auth_helpers(
        "auth/iframe", request, agent_runtime=_ctx(),
    )
    assert resp.status_code == 200
    method, url, headers = upstream.requests[0]
    assert url.startswith("https://proj-x.firebaseapp.com/__/auth/iframe")
    assert "apiKey=AIzaX" in url
    # Origin-bound headers never cross the proxy, either direction.
    assert "cookie" not in {k.lower() for k in headers}
    assert "set-cookie" not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_rejects_paths_outside_the_helper_namespace():
    request = _request(firebase=_firebase(), upstream=_FakeUpstream())
    with pytest.raises(HTTPException) as err:
        await firebase_auth_proxy.firebase_auth_helpers(
            "not-a-helper", request, agent_runtime=_ctx(),
        )
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_404_when_project_has_no_firebase():
    request = _request(upstream=_FakeUpstream())
    with pytest.raises(HTTPException) as err:
        await firebase_auth_proxy.firebase_auth_helpers(
            "auth/iframe", request, agent_runtime=_ctx(),
        )
    assert err.value.status_code == 404
