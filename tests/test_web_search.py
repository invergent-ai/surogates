from __future__ import annotations

import httpx
import pytest

from surogates.tools.builtin import web_search


@pytest.mark.asyncio
async def test_tavily_request_uses_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"results": []}

    class FakeAsyncClient:
        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str] | None = None,
            timeout: int,
        ) -> FakeResponse:
            captured["url"] = url
            captured["json"] = dict(json)
            captured["headers"] = dict(headers or {})
            captured["timeout"] = timeout
            return FakeResponse()

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    payload = {"url": "https://example.com"}

    await web_search._tavily_request("crawl", payload)

    assert captured["url"] == "https://api.tavily.com/crawl"
    assert captured["headers"] == {"Authorization": "Bearer tvly-test-key"}
    assert captured["json"] == {"url": "https://example.com"}
    assert payload == {"url": "https://example.com"}


def test_web_search_schema_exposes_limit() -> None:
    """The model can only widen a search if ``limit`` is in the schema.

    The handler has always honoured a ``limit`` argument, but it was
    absent from the advertised parameters, so every search silently fell
    back to the default of 5 results.
    """
    props = web_search.WEB_SEARCH_SCHEMA_PARAMS["properties"]
    assert "limit" in props
    assert props["limit"]["type"] == "integer"
    assert props["limit"]["maximum"] == web_search._MAX_SEARCH_LIMIT


@pytest.mark.asyncio
async def test_web_search_handler_forwards_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_tavily_search(query: str, limit: int = web_search._DEFAULT_SEARCH_LIMIT) -> dict:
        captured["query"] = query
        captured["limit"] = limit
        return {"success": True, "data": {"web": []}}

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(web_search, "_get_backend", lambda: "tavily")
    monkeypatch.setattr(web_search, "_tavily_search", fake_tavily_search)

    await web_search._web_search_handler({"query": "qubits", "limit": 20})

    assert captured["limit"] == 20


@pytest.mark.asyncio
async def test_web_search_handler_caps_limit_at_max(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_tavily_search(query: str, limit: int = web_search._DEFAULT_SEARCH_LIMIT) -> dict:
        captured["limit"] = limit
        return {"success": True, "data": {"web": []}}

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(web_search, "_get_backend", lambda: "tavily")
    monkeypatch.setattr(web_search, "_tavily_search", fake_tavily_search)

    await web_search._web_search_handler({"query": "x", "limit": 999})

    assert captured["limit"] == web_search._MAX_SEARCH_LIMIT
