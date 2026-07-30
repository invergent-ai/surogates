"""Tests for the WhatsApp Cloud API Graph client.

Written BEFORE the implementation module exists (TDD).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from surogates.channels.platforms.whatsapp_api import (
    DEFAULT_API_VERSION,
    GRAPH_API_BASE,
    download_media,
    ext_for_mime,
    format_graph_error,
    graph_url,
    send_message,
    upload_media,
)

TOKEN = "EAAtest-token"
PNID = "7794189252778687"


# ---------------------------------------------------------------------------
# graph_url
# ---------------------------------------------------------------------------


class TestGraphUrl:
    def test_builds_phone_scoped_url(self):
        assert graph_url(PNID, "messages") == (
            f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/{PNID}/messages"
        )

    def test_strips_leading_slash(self):
        assert graph_url(PNID, "/media").endswith(f"/{PNID}/media")

    def test_version_is_not_v20(self):
        # v20.0 is removed by Meta on 2026-09-24.
        assert DEFAULT_API_VERSION != "v20.0"

    def test_version_override(self):
        assert "/v25.0/" in graph_url(PNID, "messages", api_version="v25.0")


# ---------------------------------------------------------------------------
# format_graph_error
# ---------------------------------------------------------------------------


class TestFormatGraphError:
    def test_includes_code_and_status(self):
        body = {"error": {"message": "Re-engagement message", "code": 131047}}
        assert format_graph_error(400, body) == (
            "graph error 131047 (HTTP 400): Re-engagement message"
        )

    def test_codeless_error_has_no_graph_prefix(self):
        # Code-less errors must never match a permanent prefix, so they stay
        # retryable by construction.
        body = {"error": {"message": "boom"}}
        out = format_graph_error(500, body)
        assert out == "HTTP 500: boom"
        assert "graph error" not in out

    def test_missing_error_object(self):
        assert format_graph_error(502, {}) == "HTTP 502: unknown error"


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_returns_wamid_on_success(self):
        url = graph_url(PNID, "messages")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(url).mock(
                    return_value=httpx.Response(
                        200, json={"messages": [{"id": "wamid.OUT1"}]},
                    )
                )
                wamid, error = await send_message(
                    client,
                    token=TOKEN,
                    phone_number_id=PNID,
                    payload={"type": "text"},
                    api_version=DEFAULT_API_VERSION,
                )
        assert wamid == "wamid.OUT1"
        assert error is None

    @pytest.mark.asyncio
    async def test_sends_bearer_token(self):
        url = graph_url(PNID, "messages")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                route = router.post(url).mock(
                    return_value=httpx.Response(
                        200, json={"messages": [{"id": "wamid.X"}]},
                    )
                )
                await send_message(
                    client, token=TOKEN, phone_number_id=PNID,
                    payload={}, api_version=DEFAULT_API_VERSION,
                )
        assert route.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio
    async def test_returns_formatted_error_on_failure(self):
        url = graph_url(PNID, "messages")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(url).mock(
                    return_value=httpx.Response(
                        400, json={"error": {"message": "bad", "code": 100}},
                    )
                )
                wamid, error = await send_message(
                    client, token=TOKEN, phone_number_id=PNID,
                    payload={}, api_version=DEFAULT_API_VERSION,
                )
        assert wamid is None
        assert error == "graph error 100 (HTTP 400): bad"


# ---------------------------------------------------------------------------
# download_media — the two-hop fetch
# ---------------------------------------------------------------------------


class TestDownloadMedia:
    @pytest.mark.asyncio
    async def test_two_hop_fetch_returns_bytes_and_mime(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_abc"
        blob_url = "https://lookaside.fbsbx.com/whatsapp/m/xyz"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(meta_url).mock(
                    return_value=httpx.Response(
                        200,
                        json={"url": blob_url, "mime_type": "image/jpeg", "file_size": 9},
                    )
                )
                blob = router.get(blob_url).mock(
                    return_value=httpx.Response(200, content=b"JPEGBYTES")
                )
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_abc",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data == b"JPEGBYTES"
        assert mime == "image/jpeg"
        # The signed lookaside URL still requires the bearer token.
        assert blob.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"

    @pytest.mark.asyncio
    async def test_oversize_rejected_before_body_fetch(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_big"
        blob_url = "https://lookaside.fbsbx.com/whatsapp/m/big"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=False) as router:
                router.get(meta_url).mock(
                    return_value=httpx.Response(
                        200,
                        json={"url": blob_url, "mime_type": "video/mp4",
                              "file_size": 99_000_000},
                    )
                )
                blob = router.get(blob_url).mock(
                    return_value=httpx.Response(200, content=b"never")
                )
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_big",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data is None
        assert len(blob.calls) == 0, "body was fetched despite exceeding the cap"

    @pytest.mark.asyncio
    async def test_expired_url_retries_metadata_once(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_exp"
        stale = "https://lookaside.fbsbx.com/whatsapp/m/stale"
        fresh = "https://lookaside.fbsbx.com/whatsapp/m/fresh"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(meta_url).mock(
                    side_effect=[
                        httpx.Response(200, json={"url": stale, "mime_type": "image/png",
                                                  "file_size": 4}),
                        httpx.Response(200, json={"url": fresh, "mime_type": "image/png",
                                                  "file_size": 4}),
                    ]
                )
                router.get(stale).mock(return_value=httpx.Response(410))
                router.get(fresh).mock(return_value=httpx.Response(200, content=b"PNG!"))
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_exp",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data == b"PNG!"

    @pytest.mark.asyncio
    async def test_metadata_failure_returns_none_pair(self):
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_gone"
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.get(meta_url).mock(return_value=httpx.Response(404, json={}))
                data, mime = await download_media(
                    client, token=TOKEN, media_id="media_gone",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data is None
        assert mime is None

    @pytest.mark.asyncio
    async def test_path_shaped_media_id_rejected_without_request(self):
        # The media id lands in a Graph URL path component; a path-shaped id
        # must be refused before any HTTP (spec §6.5: sanitize the
        # Meta-supplied media_id before it reaches any path component).
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=False):
                data, mime = await download_media(
                    client, token=TOKEN, media_id="../7794/messages",
                    max_bytes=1024, api_version=DEFAULT_API_VERSION,
                )
        assert data is None
        assert mime is None


# ---------------------------------------------------------------------------
# upload_media
# ---------------------------------------------------------------------------


class TestUploadMedia:
    @pytest.mark.asyncio
    async def test_returns_media_id(self):
        url = graph_url(PNID, "media")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=True) as router:
                router.post(url).mock(
                    return_value=httpx.Response(200, json={"id": "media_new"})
                )
                media_id, error = await upload_media(
                    client, token=TOKEN, phone_number_id=PNID,
                    filename="a.png", data=b"x", mime_type="image/png",
                    api_version=DEFAULT_API_VERSION,
                )
        assert media_id == "media_new"
        assert error is None

    @pytest.mark.asyncio
    async def test_oversize_rejected_without_request(self):
        url = graph_url(PNID, "media")
        async with httpx.AsyncClient() as client:
            with respx.mock(assert_all_called=False) as router:
                route = router.post(url).mock(
                    return_value=httpx.Response(200, json={"id": "nope"})
                )
                media_id, error = await upload_media(
                    client, token=TOKEN, phone_number_id=PNID,
                    filename="big.png", data=b"x" * (6 * 1024 * 1024),
                    mime_type="image/png", api_version=DEFAULT_API_VERSION,
                )
        assert media_id is None
        assert "cap" in (error or "")
        assert len(route.calls) == 0


# ---------------------------------------------------------------------------
# ext_for_mime
# ---------------------------------------------------------------------------


class TestExtForMime:
    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("audio/ogg", ".ogg"),                 # not mimetypes' .oga
            ("audio/ogg; codecs=opus", ".ogg"),    # parameters stripped
            ("audio/x-opus+ogg", ".ogg"),
            ("audio/opus", ".ogg"),
            ("audio/mp4", ".m4a"),                 # iOS voice memos
            ("audio/x-m4a", ".m4a"),
            ("image/jpeg", ".jpg"),                # not legacy .jpe
        ],
    )
    def test_overrides(self, mime, expected):
        assert ext_for_mime(mime) == expected

    def test_unknown_mime_falls_back_to_bin(self):
        assert ext_for_mime("application/x-nonsense") == ".bin"
