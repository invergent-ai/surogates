"""Tests for ``surogates.runtime.FirebaseConfig`` + PlatformClient method.

Plan 1b / Task 6.  Pure-projection dataclass mirroring the management
plane's FirebaseConfigResponse plus the PlatformClient method that
fetches it.  Cache + lifespan wiring in Tasks 7-8.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest


def test_firebase_config_dataclass_required_fields():
    from surogates.runtime import FirebaseConfig

    cfg = FirebaseConfig(
        project_id="p-1",
        firebase_project_id="fb-1",
        api_key="k",
        auth_domain="d",
        enabled_providers=("google",),
    )
    assert cfg.project_id == "p-1"
    assert cfg.firebase_project_id == "fb-1"
    assert cfg.api_key == "k"
    assert cfg.auth_domain == "d"
    assert cfg.enabled_providers == ("google",)


def test_firebase_config_optional_fields_default_none():
    from surogates.runtime import FirebaseConfig

    cfg = FirebaseConfig(
        project_id="p-1",
        firebase_project_id="fb-1",
        api_key="k",
        auth_domain="d",
        enabled_providers=(),
    )
    assert cfg.app_id is None
    assert cfg.messaging_sender_id is None
    assert cfg.measurement_id is None


def test_firebase_config_is_frozen():
    from surogates.runtime import FirebaseConfig

    cfg = FirebaseConfig(
        project_id="p-1",
        firebase_project_id="fb-1",
        api_key="k",
        auth_domain="d",
        enabled_providers=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.api_key = "x"  # type: ignore[misc]


def test_firebase_config_enabled_providers_must_be_tuple():
    """``enabled_providers`` is a tuple so the frozen dataclass cannot
    be mutated through the field (mirrors ``mcp_server_ids`` on
    AgentRuntimeContext)."""
    from surogates.runtime import FirebaseConfig

    cfg = FirebaseConfig(
        project_id="p-1",
        firebase_project_id="fb-1",
        api_key="k",
        auth_domain="d",
        enabled_providers=("google", "password"),
    )
    assert isinstance(cfg.enabled_providers, tuple)


@pytest.mark.asyncio
async def test_platform_client_get_firebase_config_happy_path():
    from surogates.runtime import PlatformClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/projects/p-1/firebase-config"
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(
            200,
            json={
                "project_id": "p-1",
                "firebase_project_id": "fb-1",
                "api_key": "k",
                "auth_domain": "d",
                "app_id": "a",
                "messaging_sender_id": "s",
                "measurement_id": None,
                "enabled_providers": ["google", "password"],
            },
        )

    client = PlatformClient(
        base_url="https://ops", token="t",
        transport=httpx.MockTransport(handler),
    )
    try:
        cfg = await client.get_firebase_config("p-1")
    finally:
        await client.aclose()

    assert cfg["api_key"] == "k"
    assert cfg["enabled_providers"] == ["google", "password"]


@pytest.mark.asyncio
async def test_platform_client_get_firebase_config_404_raises_lookup_error():
    from surogates.runtime import PlatformClient

    def handler(_request):
        return httpx.Response(404, json={"detail": "no firebase"})

    client = PlatformClient(
        base_url="https://ops", token="t",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(LookupError) as excinfo:
            await client.get_firebase_config("p-1")
    finally:
        await client.aclose()

    assert "p-1" in str(excinfo.value)


@pytest.mark.asyncio
async def test_platform_client_get_firebase_config_401_raises_platform_auth_error():
    from surogates.runtime import PlatformAuthError, PlatformClient

    def handler(_request):
        return httpx.Response(401, json={"detail": "bad"})

    client = PlatformClient(
        base_url="https://ops", token="bad",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PlatformAuthError):
            await client.get_firebase_config("p-1")
    finally:
        await client.aclose()
