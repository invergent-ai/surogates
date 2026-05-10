"""Async HTTP client for kernel-images REST API."""

from __future__ import annotations

from typing import Any

import httpx


class KernelBrowserClient:
    """HTTP client for one kernel-images browser REST endpoint."""

    def __init__(
        self,
        rest_url: str,
        *,
        timeout: float = 30.0,
        snapshot_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rest_url = rest_url.rstrip("/")
        self._timeout = timeout
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.rest_url,
            timeout=timeout,
        )
        self._closed = False
        self._snapshot_cache = snapshot_cache if snapshot_cache is not None else {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        if self._closed:
            return
        await self._http.aclose()
        self._closed = True

    async def __aenter__(self) -> "KernelBrowserClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]:
        """Navigate to a URL and return the final URL and title."""

        code = (
            "await page.goto({url!r}, {{waitUntil: {wait_until!r}}});\n"
            "return {{ url: page.url(), title: await page.title() }};"
        ).format(url=url, wait_until=wait_until)
        result = await self._playwright_execute(code)
        self._invalidate_snapshot_cache()
        return result

    async def _playwright_execute(
        self,
        code: str,
        *,
        timeout_sec: int = 60,
    ) -> Any:
        """POST to /playwright/execute and unwrap kernel-images' envelope."""

        response = await self._http.post(
            "/playwright/execute",
            json={"code": code, "timeout_sec": timeout_sec},
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success", False):
            raise RuntimeError(body.get("error") or "playwright execute failed")
        return body.get("result")

    def _invalidate_snapshot_cache(self) -> None:
        self._snapshot_cache.clear()
