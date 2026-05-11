"""Builtin agent-browser tools."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

from surogates.browser.base import (
    BrowserEndpoint,
    BrowserSpec,
    BrowserUnavailableError,
    browser_unavailable_result,
)
from surogates.browser.client import KernelBrowserClient
from surogates.browser.control import BrowserControlStore
from surogates.browser.pool import BrowserPool
from surogates.storage.tenant import session_workspace_key
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)


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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
) -> tuple[str, BrowserEndpoint, dict[str, dict[str, Any]]] | str:
    if browser_pool is None or session_id is None:
        return browser_unavailable_result("browser pool not configured")

    sid = str(session_id)
    if browser_control is not None and await browser_control.get(sid) is not None:
        return _paused_by_user_result()

    try:
        storage_bucket = (session_config or {}).get("storage_bucket")
        browser_spec = spec or BrowserSpec(
            workspace_path=workspace_path,
            workspace_source_ref=(
                f"s3://{storage_bucket}/sessions/{sid}"
                if storage_bucket
                else None
            ),
        )
        result = await browser_pool.ensure(
            session_id=sid,
            org_id=str(getattr(tenant, "org_id", "")) if tenant is not None else "",
            user_id=str(getattr(tenant, "user_id", "")) if tenant is not None else "",
            spec=browser_spec,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
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
        workspace_path=workspace_path,
        session_config=session_config,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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
        "x": {
            "type": "integer",
            "description": "Viewport x coordinate where the scroll wheel event should occur.",
        },
        "y": {
            "type": "integer",
            "description": "Viewport y coordinate where the scroll wheel event should occur.",
        },
        "delta_x": {
            "type": "integer",
            "default": 0,
            "description": "Horizontal wheel delta. Positive values scroll right; negative values scroll left.",
        },
        "delta_y": {
            "type": "integer",
            "default": 0,
            "description": "Vertical wheel delta. Positive values scroll down; negative values scroll up.",
        },
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
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

_SCREENSHOT_DIR = "browser-screenshots"


def _new_screenshot_path() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"browser-screenshot-{timestamp}-{uuid4().hex[:8]}.png"
    return f"{_SCREENSHOT_DIR}/{filename}"


def _workspace_result_path(
    *,
    workspace_path: str | None,
    storage_bucket: str | None,
    relative_path: str,
) -> str:
    root = workspace_path or ("/workspace" if storage_bucket else "")
    if not root:
        return relative_path
    return f"{root.rstrip('/')}/{relative_path}"


def _save_screenshot_to_workspace(
    png_bytes: bytes,
    *,
    workspace_path: str | None,
    relative_path: str,
) -> str | None:
    if not workspace_path:
        return None

    workspace = Path(workspace_path).resolve()
    target = workspace / relative_path
    target_dir = target.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png_bytes)
    except OSError as exc:
        logger.warning(
            "Could not save browser screenshot to workspace path %s: %s",
            target,
            exc,
        )
        return None
    return relative_path


async def _save_screenshot_to_storage(
    png_bytes: bytes,
    *,
    storage: Any | None,
    session_id: UUID | str | None,
    session_config: dict[str, Any] | None,
    relative_path: str,
) -> str | None:
    if storage is None or session_id is None:
        return None

    storage_bucket = (session_config or {}).get("storage_bucket")
    if not storage_bucket:
        return None

    key = session_workspace_key(session_id, relative_path)
    try:
        await storage.write(storage_bucket, key, png_bytes)
    except Exception as exc:
        logger.warning(
            "Could not save browser screenshot to workspace storage %s/%s: %s",
            storage_bucket,
            key,
            exc,
        )
        return None
    return relative_path


async def _browser_screenshot_handler(
    arguments: dict[str, Any],
    *,
    tenant: Any = None,
    session_id: UUID | str | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    _client_factory: Callable[..., Any] = _default_client_factory,
    workspace_path: str | None = None,
    session_config: dict[str, Any] | None = None,
    storage: Any | None = None,
    **_: Any,
) -> str:
    preflight = await _resolve_session_browser(
        tenant=tenant,
        session_id=session_id,
        browser_pool=browser_pool,
        browser_control=browser_control,
        workspace_path=workspace_path,
        session_config=session_config,
    )
    if isinstance(preflight, str):
        return preflight

    _browser_id, endpoint, snapshot_cache = preflight
    storage_bucket = (session_config or {}).get("storage_bucket")
    should_save = bool(workspace_path or (storage is not None and storage_bucket))
    if not should_save:
        return json.dumps(
            {
                "error": "workspace_unavailable",
                "detail": "browser_screenshot requires a session workspace destination",
            }
        )

    relative_path = _new_screenshot_path()
    browser_save_path = f"/workspace/{relative_path}" if workspace_path else None
    result_path = _workspace_result_path(
        workspace_path=workspace_path,
        storage_bucket=storage_bucket,
        relative_path=relative_path,
    )
    saved_in_browser = browser_save_path is not None
    client = _make_client(_client_factory, endpoint, snapshot_cache)
    async with client:
        try:
            result = await client.screenshot(
                region=arguments.get("region"),
                annotate=bool(arguments.get("annotate", False)),
                save_path=browser_save_path,
            )
        except RuntimeError:
            if browser_save_path is None:
                raise
            saved_in_browser = False
            result = await client.screenshot(
                region=arguments.get("region"),
                annotate=bool(arguments.get("annotate", False)),
            )

    png_bytes = result["png_bytes"]
    saved = await _save_screenshot_to_storage(
        png_bytes,
        storage=storage,
        session_id=session_id,
        session_config=session_config,
        relative_path=relative_path,
    )
    if saved is None and not saved_in_browser:
        saved = _save_screenshot_to_workspace(
            png_bytes,
            workspace_path=workspace_path,
            relative_path=relative_path,
        )
    if saved is None and saved_in_browser:
        saved = relative_path

    if saved is None:
        return json.dumps(
            {
                "error": "screenshot_save_failed",
                "bytes": len(png_bytes),
                "mime_type": "image/png",
                "detail": "Screenshot was captured but could not be saved to the session workspace.",
            }
        )

    body: dict[str, Any] = {
        "saved": True,
        "path": result_path,
        "relative_path": relative_path,
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
            description=(
                "Scroll at viewport coordinates. Use positive delta_y to scroll down "
                "and negative delta_y to scroll up."
            ),
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
                "Capture a PNG screenshot and save it to the session workspace. "
                "Use annotate=true to overlay numbered labels for cached refs."
            ),
            parameters=SCREENSHOT_SCHEMA,
        ),
        handler=_browser_screenshot_handler,
        toolset="browser",
    )
