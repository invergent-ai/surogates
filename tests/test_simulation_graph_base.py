"""Only a simulation phone-number id, on a deployment running Simulation, leaves Meta."""

from __future__ import annotations

import httpx
import pytest
import respx

from surogates.channels.platforms.whatsapp_api import (
    GRAPH_API_BASE,
    download_media,
    graph_base,
    graph_url,
)

REAL = "100000000000001"
SIMULATION = "9991234567890123"


def test_real_id_always_reaches_meta(monkeypatch):
    monkeypatch.setenv("SUROGATES_SIMULATION_URL", "http://simulation:8004")
    assert graph_base(REAL) == GRAPH_API_BASE
    assert graph_url(REAL, "messages").startswith(GRAPH_API_BASE + "/")


def test_simulation_id_reaches_its_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_SIMULATION_URL", "http://simulation:8004/")
    assert graph_base(SIMULATION) == f"http://simulation:8004/w/{SIMULATION}/whatsapp"
    assert graph_url(SIMULATION, "messages") == (
        f"http://simulation:8004/w/{SIMULATION}/whatsapp/v23.0/{SIMULATION}/messages"
    )


def test_simulation_id_without_simulation_deployment_reaches_meta(monkeypatch):
    monkeypatch.delenv("SUROGATES_SIMULATION_URL", raising=False)
    assert graph_base(SIMULATION) == GRAPH_API_BASE


@pytest.mark.asyncio
async def test_download_media_uses_the_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_SIMULATION_URL", "http://simulation:8004")
    world = f"http://simulation:8004/w/{SIMULATION}/whatsapp"
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=True) as router:
            router.get(f"{world}/v23.0/media7").mock(return_value=httpx.Response(
                200, json={"url": f"{world}/media-bytes/media7", "mime_type": "image/png", "file_size": 3},
            ))
            router.get(f"{world}/media-bytes/media7").mock(
                return_value=httpx.Response(200, content=b"png"),
            )
            data, mime = await download_media(
                client, token="t", media_id="media7", max_bytes=100,
                phone_number_id=SIMULATION,
            )
    assert (data, mime) == (b"png", "image/png")
