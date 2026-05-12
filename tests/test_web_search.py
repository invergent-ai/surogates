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
