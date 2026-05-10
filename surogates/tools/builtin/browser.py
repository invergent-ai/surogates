"""Builtin agent-browser tools."""

from __future__ import annotations

import json
from typing import Any, Callable
from uuid import UUID

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserUnavailableError,
    browser_unavailable_result,
)
from surogates.browser.client import KernelBrowserClient
from surogates.browser.control import BrowserControlStore
from surogates.browser.pool import BrowserPool
from surogates.tools.registry import ToolRegistry, ToolSchema


def _paused_by_user_result() -> str:
    return json.dumps(
        {
            "error": "paused_by_user",
            "guidance": (
                "The user has taken control of the browser. Wait for them to "
                "finish before continuing; every browser_* tool will return "
                "this error until they release control."
            ),
        }
    )


async def _resolve_session_browser(
    *,
    tenant: Any,
    session_id: UUID | str | None,
    browser_pool: BrowserPool | None,
    browser_control: BrowserControlStore | None,
    spec: BrowserSpec | None = None,
) -> tuple[str, BrowserEndpoint, dict[str, dict[str, Any]]] | str:
    if browser_pool is None or session_id is None:
        return browser_unavailable_result("browser pool not configured")

    sid = str(session_id)
    if browser_control is not None and await browser_control.get(sid) is not None:
        return _paused_by_user_result()

    try:
        result = await browser_pool.ensure(
            session_id=sid,
            org_id=str(getattr(tenant, "org_id", "")) if tenant is not None else "",
            user_id=str(getattr(tenant, "user_id", "")) if tenant is not None else "",
            spec=spec or BrowserSpec(),
        )
    except BrowserUnavailableError as exc:
        return browser_unavailable_result(exc.reason)
    return result.browser_id, result.endpoint, result.snapshot_cache


def _default_client_factory(
    endpoint: BrowserEndpoint,
    snapshot_cache: dict[str, dict[str, Any]],
) -> KernelBrowserClient:
    return KernelBrowserClient(rest_url=endpoint.rest_url, snapshot_cache=snapshot_cache)


def _make_client(
    factory: Callable[..., Any],
    endpoint: BrowserEndpoint,
    snapshot_cache: dict[str, dict[str, Any]],
) -> Any:
    try:
        return factory(endpoint, snapshot_cache)
    except TypeError as first_error:
        try:
            return factory(endpoint)
        except TypeError:
            raise first_error


NAVIGATE_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to navigate to."},
        "wait_until": {
            "type": "string",
            "enum": ["load", "domcontentloaded", "networkidle"],
            "default": "load",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}


async def _browser_navigate_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
    )
    if isinstance(preflight, str):
        return preflight

    _browser_id, endpoint, snapshot_cache = preflight
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    try:
        async with client:
            result = await client.navigate(
                arguments["url"],
                wait_until=arguments.get("wait_until", "load"),
            )
    except RuntimeError as exc:
        return json.dumps({"error": "navigate_failed", "detail": str(exc)})
    return json.dumps({"url": result["url"], "title": result["title"]})


GET_STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "interactive_only": {"type": "boolean", "default": False},
        "compact": {"type": "boolean", "default": False},
        "max_depth": {"type": "integer", "minimum": 0},
        "selector": {"type": "string"},
    },
    "additionalProperties": False,
}


async def _browser_get_state_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
    )
    if isinstance(preflight, str):
        return preflight

    _browser_id, endpoint, snapshot_cache = preflight
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        state = await client.get_state(
            interactive_only=arguments.get("interactive_only", False),
            compact=arguments.get("compact", False),
            max_depth=arguments.get("max_depth"),
            selector=arguments.get("selector"),
        )
    return json.dumps(state)


CLOSE_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


async def _browser_close_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    **_: Any,
) -> str:
    if browser_pool is None or session_id is None:
        return json.dumps({"closed": False, "reason": "no_browser_pool"})

    sid = str(session_id)
    if browser_control is not None and await browser_control.get(sid) is not None:
        return _paused_by_user_result()

    await browser_pool.destroy_for_session(sid)
    return json.dumps({"closed": True})


def register(registry: ToolRegistry) -> None:
    registry.register(
        name="browser_navigate",
        schema=ToolSchema(
            name="browser_navigate",
            description=(
                "Navigate the agent's browser to a URL and return the final URL "
                "and page title."
            ),
            parameters=NAVIGATE_SCHEMA,
        ),
        handler=_browser_navigate_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_get_state",
        schema=ToolSchema(
            name="browser_get_state",
            description=(
                "Return the current page tree with @eN refs for browser_click "
                "and browser_type."
            ),
            parameters=GET_STATE_SCHEMA,
        ),
        handler=_browser_get_state_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_close",
        schema=ToolSchema(
            name="browser_close",
            description="Close the browser for this session.",
            parameters=CLOSE_SCHEMA,
        ),
        handler=_browser_close_handler,
        toolset="browser",
    )
