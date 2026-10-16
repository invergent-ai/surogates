"""The worker's client for runtime-office: requests, and errors told apart."""

from __future__ import annotations

import json

import httpx
import pytest

from surogates.config import OfficeSettings
from surogates.office import runtime_office
from surogates.office.runtime_office import OfficeBusy, PageGone, RuntimeOffice, RuntimeOfficeError, page_id

OFFICE = OfficeSettings(
    public_url="https://office.example", internal_url="http://docs.internal",
    callback_base_url="https://api.example", jwt_secret="j" * 32, token_secret="t" * 32,
    runtime_office_url="http://runtime-office.test/", runtime_office_token="r" * 32,
)


@pytest.fixture
def served(monkeypatch):
    """Answer every request with (status, body); the requests made are returned."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def install(status: int, body: object) -> list[httpx.Request]:
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(status, json=body)
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(runtime_office.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
        return requests

    return install


def test_page_ids_match_runtime_office():
    assert page_id("abc", "s-1") == "abc.s-1"


async def test_open_sends_the_document_and_the_bearer(served):
    requests = served(200, {"page_id": "abc.s-1"})
    assert await RuntimeOffice(OFFICE).open(doc_key="abc", session_id="s-1", config={"a": 1}, document_server_url="http://docs") == "abc.s-1"
    assert str(requests[0].url) == "http://runtime-office.test/pages"
    assert requests[0].headers["authorization"] == "Bearer " + "r" * 32
    assert json.loads(requests[0].content) == {"doc_key": "abc", "session_id": "s-1", "config": {"a": 1}, "document_server_url": "http://docs"}


async def test_read_edit_and_flush_go_to_the_page(served):
    requests = served(200, {"flushed": True, "blocks": []})
    runtime = RuntimeOffice(OFFICE)
    await runtime.read("abc.s-1", {"view": "outline"})
    await runtime.edit("abc.s-1", [{"op": "delete"}])
    assert await runtime.flush("abc.s-1") is True
    assert [str(r.url) for r in requests] == [
        "http://runtime-office.test/pages/abc.s-1/read",
        "http://runtime-office.test/pages/abc.s-1/edit",
        "http://runtime-office.test/pages/abc.s-1/flush",
    ]
    assert json.loads(requests[1].content) == {"ops": [{"op": "delete"}]}


@pytest.mark.parametrize(("status", "error"), [(503, OfficeBusy), (410, PageGone), (404, PageGone), (502, RuntimeOfficeError)])
async def test_errors_are_told_apart(served, status, error):
    served(status, {"error": "said so"})
    with pytest.raises(error, match="said so") as raised:
        await RuntimeOffice(OFFICE).read("abc.s-1", {"view": "outline"})
    assert raised.value.status == status


async def test_a_wrong_request_keeps_its_words(served):
    served(400, {"error": "op 0: unknown op"})
    with pytest.raises(RuntimeOfficeError, match="op 0: unknown op") as raised:
        await RuntimeOffice(OFFICE).edit("abc.s-1", [])
    assert raised.value.status == 400


async def test_close_deletes_the_page_and_close_pages_closes_each_chats(served):
    requests = served(204, {})
    await runtime_office.close_pages(OFFICE, "abc", ["s-1", "s-2"])
    assert [(r.method, str(r.url)) for r in requests] == [
        ("DELETE", "http://runtime-office.test/pages/abc.s-1"), ("DELETE", "http://runtime-office.test/pages/abc.s-2"),
    ]


async def test_an_unreachable_service_is_an_error(monkeypatch):
    real_client = httpx.AsyncClient

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(runtime_office.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(refuse), **kwargs))
    with pytest.raises(RuntimeOfficeError, match="could not be reached"):
        await RuntimeOffice(OFFICE).flush("abc.s-1")


async def test_an_edit_waits_longer_and_its_timeout_says_it_may_have_applied(monkeypatch):
    real_client = httpx.AsyncClient
    timeouts: list[float] = []

    def too_slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    def client(**kwargs):
        timeouts.append(kwargs["timeout"])
        return real_client(transport=httpx.MockTransport(too_slow), **kwargs)

    monkeypatch.setattr(runtime_office.httpx, "AsyncClient", client)
    with pytest.raises(RuntimeOfficeError, match="did not answer in time; the edit may have applied") as raised:
        await RuntimeOffice(OFFICE).edit("abc.s-1", [{"op": "delete"}])
    assert raised.value.status == 0 and not isinstance(raised.value, PageGone)
    with pytest.raises(RuntimeOfficeError, match="could not be reached"):
        await RuntimeOffice(OFFICE).read("abc.s-1", {"view": "outline"})
    # runtime-office's slowest edit (a deck's ten slides measured, then its commands and the flush) is past 60 s.
    assert timeouts == [300.0, 60.0]
