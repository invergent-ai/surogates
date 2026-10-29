"""Only a rehearsal phone-number id, on a deployment running Rehearsal, leaves Meta."""

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
REHEARSAL = "9991234567890123"


def test_real_id_always_reaches_meta(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004")
    assert graph_base(REAL) == GRAPH_API_BASE
    assert graph_url(REAL, "messages").startswith(GRAPH_API_BASE + "/")


def test_rehearsal_id_reaches_its_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004/")
    assert graph_base(REHEARSAL) == f"http://rehearsal:8004/w/{REHEARSAL}/whatsapp"
    assert graph_url(REHEARSAL, "messages") == (
        f"http://rehearsal:8004/w/{REHEARSAL}/whatsapp/v23.0/{REHEARSAL}/messages"
    )


def test_rehearsal_id_without_rehearsal_deployment_reaches_meta(monkeypatch):
    monkeypatch.delenv("SUROGATES_REHEARSAL_URL", raising=False)
    assert graph_base(REHEARSAL) == GRAPH_API_BASE


@pytest.mark.asyncio
async def test_download_media_uses_the_world(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://rehearsal:8004")
    world = f"http://rehearsal:8004/w/{REHEARSAL}/whatsapp"
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
                phone_number_id=REHEARSAL,
            )
    assert (data, mime) == (b"png", "image/png")
