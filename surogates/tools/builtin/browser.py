"""Builtin agent-browser tools."""

from __future__ import annotations

import base64
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
        try:
            state = await client.get_state(
                interactive_only=arguments.get("interactive_only", False),
                compact=arguments.get("compact", False),
                max_depth=arguments.get("max_depth"),
                selector=arguments.get("selector"),
            )
        except RuntimeError as exc:
            return json.dumps({"error": "get_state_failed", "detail": str(exc)})
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


CLICK_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "pattern": "^@e[0-9]+$"},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
        "click_type": {"type": "string", "enum": ["click", "down", "up"], "default": "click"},
        "num_clicks": {"type": "integer", "minimum": 1, "default": 1},
    },
    "additionalProperties": False,
}


async def _browser_click_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    **_: Any,
) -> str:
    has_ref = "ref" in arguments
    has_coords = "x" in arguments and "y" in arguments
    if not has_ref and not has_coords:
        return json.dumps({"error": "invalid_arguments", "detail": "click requires ref or x/y"})

    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
    )
    if isinstance(preflight, str):
        return preflight

    _browser_id, endpoint, snapshot_cache = preflight
    common = {
        "button": arguments.get("button", "left"),
        "click_type": arguments.get("click_type", "click"),
        "num_clicks": arguments.get("num_clicks", 1),
    }
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        try:
            if has_ref:
                await client.click_ref(arguments["ref"], **common)
            else:
                await client.click_at(arguments["x"], arguments["y"], **common)
        except KeyError as exc:
            return json.dumps({"error": "unknown_ref", "detail": str(exc)})
    return json.dumps({"clicked": True})


TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "ref": {"type": "string", "pattern": "^@e[0-9]+$"},
        "delay_ms": {"type": "integer", "minimum": 0, "default": 0},
    },
    "required": ["text"],
    "additionalProperties": False,
}


async def _browser_type_handler(
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
        if "ref" in arguments:
            await client.type_into_ref(
                arguments["ref"],
                arguments["text"],
                delay_ms=arguments.get("delay_ms", 0),
            )
        else:
            await client.type_text(arguments["text"], delay_ms=arguments.get("delay_ms", 0))
    return json.dumps({"typed": True})


PRESS_KEY_SCHEMA = {
    "type": "object",
    "properties": {
        "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "duration_ms": {"type": "integer", "minimum": 0, "default": 0},
    },
    "required": ["keys"],
    "additionalProperties": False,
}


async def _browser_press_key_handler(
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
        await client.press_key(*arguments["keys"], duration_ms=arguments.get("duration_ms", 0))
    return json.dumps({"pressed": arguments["keys"]})


SCROLL_SCHEMA = {
    "type": "object",
    "properties": {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "delta_x": {"type": "integer", "default": 0},
        "delta_y": {"type": "integer", "default": 0},
    },
    "required": ["x", "y"],
    "additionalProperties": False,
}


async def _browser_scroll_handler(
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
        await client.scroll_at(
            arguments["x"],
            arguments["y"],
            delta_x=arguments.get("delta_x", 0),
            delta_y=arguments.get("delta_y", 0),
        )
    return json.dumps({"scrolled": True})


DRAG_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "integer"},
            },
        },
        "button": {"type": "string", "enum": ["left", "middle", "right"], "default": "left"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


async def _browser_drag_handler(
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
    path = [(int(point[0]), int(point[1])) for point in arguments["path"]]
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        await client.drag(path, button=arguments.get("button", "left"))
    return json.dumps({"dragged": True, "points": len(path)})


WAIT_SCHEMA = {
    "type": "object",
    "properties": {"ms": {"type": "integer", "minimum": 0, "maximum": 30_000}},
    "required": ["ms"],
    "additionalProperties": False,
}


async def _browser_wait_handler(
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
    ms = min(int(arguments.get("ms", 0)), 30_000)
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        await client.wait(ms)
    return json.dumps({"waited_ms": ms})


SCREENSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "annotate": {"type": "boolean", "default": False},
        "region": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            "required": ["x", "y", "width", "height"],
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

_MAX_BASE64_BYTES = 256 * 1024


async def _browser_screenshot_handler(
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
        result = await client.screenshot(
            region=arguments.get("region"),
            annotate=bool(arguments.get("annotate", False)),
        )

    png_bytes = result["png_bytes"]
    if len(png_bytes) > _MAX_BASE64_BYTES:
        return json.dumps(
            {
                "error": "screenshot_too_large_for_base64",
                "bytes": len(png_bytes),
                "guidance": "Capture a smaller region or retry without annotation.",
            }
        )

    body: dict[str, Any] = {
        "base64": base64.b64encode(png_bytes).decode(),
        "mime_type": "image/png",
        "bytes": len(png_bytes),
    }
    if "annotations" in result:
        body["annotations"] = result["annotations"]
    return json.dumps(body)


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
    registry.register(
        name="browser_click",
        schema=ToolSchema(
            name="browser_click",
            description="Click a browser element by @eN ref or viewport coordinates.",
            parameters=CLICK_SCHEMA,
        ),
        handler=_browser_click_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_type",
        schema=ToolSchema(
            name="browser_type",
            description="Type text at focus or into a specific @eN ref.",
            parameters=TYPE_SCHEMA,
        ),
        handler=_browser_type_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_press_key",
        schema=ToolSchema(
            name="browser_press_key",
            description="Press one or more keyboard keys or chords.",
            parameters=PRESS_KEY_SCHEMA,
        ),
        handler=_browser_press_key_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_scroll",
        schema=ToolSchema(
            name="browser_scroll",
            description="Scroll at viewport coordinates.",
            parameters=SCROLL_SCHEMA,
        ),
        handler=_browser_scroll_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_drag",
        schema=ToolSchema(
            name="browser_drag",
            description="Drag along a path of viewport coordinates.",
            parameters=DRAG_SCHEMA,
        ),
        handler=_browser_drag_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_wait",
        schema=ToolSchema(
            name="browser_wait",
            description="Wait for a bounded number of milliseconds.",
            parameters=WAIT_SCHEMA,
        ),
        handler=_browser_wait_handler,
        toolset="browser",
    )
    registry.register(
        name="browser_screenshot",
        schema=ToolSchema(
            name="browser_screenshot",
            description=(
                "Capture a bounded base64 PNG screenshot. Use annotate=true "
                "to overlay numbered labels for cached refs."
            ),
            parameters=SCREENSHOT_SCHEMA,
        ),
        handler=_browser_screenshot_handler,
        toolset="browser",
    )
