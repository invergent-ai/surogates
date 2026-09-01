"""Browser-shell protocol: the typed messages a viewer may send, and nothing else.

Security here is structural. A viewer cannot ask the browser to run JavaScript
or read cookies because no message expresses it — there is no allowlist to keep
exhaustively correct. Two things still have to be right, and both are pure
enough to test without a browser:

* ``navigate`` — ``Page.navigate`` renders ``file://`` and executes
  ``javascript:``, so the scheme allowlist is the one validation whose failure
  is a full compromise.
* ``key`` — the named-key set is an allowlist rather than a convenience. F12 and
  Ctrl+Shift+I open devtools, and devtools is JavaScript execution: exactly the
  capability this protocol exists to withhold.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Message types that act on the page, and so require the control lease.
# ``switch_tab`` is deliberately absent: it changes what a viewer watches, not
# the page, so a viewer without the lease may still look at another tab.
# ``close_tab`` is present: it mutates the agent's browser.
COMMAND_TYPES: frozenset[str] = frozenset({
    "click", "scroll", "type", "key",
    "navigate", "back", "forward", "reload", "close_tab",
})

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Measured: uncapped, the native 1890x1984 viewport gives 366 KB frames at
# 1328 KB/s; capped, 74 KB frames at 258 KB/s. PNG is not an option — it
# encodes at 0.1 fps.
SCREENCAST_PARAMS: dict[str, object] = {
    "format": "jpeg",
    "quality": 70,
    "maxWidth": 1280,
    "maxHeight": 800,
    "everyNthFrame": 1,
}

# Keys that command a page rather than type into it, with the virtual key code
# Chrome needs — a key event carrying only ``key`` does not submit a form.
# Letters and function keys are deliberately absent: no letter means no
# Ctrl+Shift+I, and no F12 means no devtools. Text goes through ``type``.
NAMED_KEYS: dict[str, int] = {
    "Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46,
    "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
}

# CDP's modifier bitmask.
_MODIFIER_BITS: dict[str, int] = {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}

MAX_TEXT = 8192
MAX_CLICKS = 3
MAX_SCROLL_DELTA = 10_000
# An Input.insertText carrying megabytes is a trivial denial of service, and a
# frame this large is never a legitimate command.
MAX_WS_MESSAGE = 64 * 1024
# Used until the first Page.getLayoutMetrics answers, so an early click scales
# against something sane rather than dividing by zero.
DEFAULT_VIEWPORT = (1280, 800)

Call = tuple[str, dict]


class ShellProtocolError(Exception):
    """A message the protocol does not accept. Never forwarded to CDP."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unit(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShellProtocolError(f"{name} must be a number between 0 and 1")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ShellProtocolError(f"{name} must be between 0 and 1")
    return number


def _point(message: dict, viewport: tuple[int, int]) -> tuple[int, int]:
    width, height = viewport
    return (
        round(_unit(message.get("x"), "x") * width),
        round(_unit(message.get("y"), "y") * height),
    )


def _modifiers(raw: object) -> int:
    if raw is None:
        return 0
    if not isinstance(raw, dict):
        raise ShellProtocolError("mods must be an object")
    bits = 0
    for name, value in raw.items():
        if name not in _MODIFIER_BITS:
            raise ShellProtocolError(f"unknown modifier {name!r}")
        if value:
            bits |= _MODIFIER_BITS[name]
    return bits


def _click(message: dict, viewport: tuple[int, int]) -> list[Call]:
    x, y = _point(message, viewport)
    button = message.get("button", "left")
    if button not in {"left", "right", "middle"}:
        raise ShellProtocolError("unsupported mouse button")
    raw_count = message.get("count", 1)
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        raise ShellProtocolError("count must be an integer")
    if not 1 <= raw_count <= MAX_CLICKS:
        raise ShellProtocolError(f"count must be 1-{MAX_CLICKS}")
    base = {
        "x": x,
        "y": y,
        "button": button,
        "clickCount": raw_count,
        "modifiers": _modifiers(message.get("mods")),
    }
    # A lone mousePressed leaves the button held and the page in a drag.
    return [
        ("Input.dispatchMouseEvent", {**base, "type": "mousePressed"}),
        ("Input.dispatchMouseEvent", {**base, "type": "mouseReleased"}),
    ]


def _scroll(message: dict, viewport: tuple[int, int]) -> list[Call]:
    x, y = _point(message, viewport)
    deltas = {}
    for axis, key in (("deltaX", "dx"), ("deltaY", "dy")):
        value = message.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ShellProtocolError(f"{key} must be a number")
        if not math.isfinite(value) or abs(value) > MAX_SCROLL_DELTA:
            raise ShellProtocolError(f"{key} is out of range")
        deltas[axis] = float(value)
    return [(
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": x, "y": y, **deltas,
         "modifiers": _modifiers(message.get("mods"))},
    )]


def _key(message: dict) -> list[Call]:
    key = message.get("key")
    if not isinstance(key, str) or key not in NAMED_KEYS:
        raise ShellProtocolError(f"key {key!r} is not one this protocol sends")
    base = {
        "key": key,
        "windowsVirtualKeyCode": NAMED_KEYS[key],
        "nativeVirtualKeyCode": NAMED_KEYS[key],
        "modifiers": _modifiers(message.get("mods")),
    }
    return [
        ("Input.dispatchKeyEvent", {**base, "type": "keyDown"}),
        ("Input.dispatchKeyEvent", {**base, "type": "keyUp"}),
    ]


def _type(message: dict) -> list[Call]:
    text = message.get("text")
    if not isinstance(text, str):
        raise ShellProtocolError("text must be a string")
    if len(text) > MAX_TEXT:
        raise ShellProtocolError("text too long")
    return [("Input.insertText", {"text": text})]


def _navigate(message: dict) -> list[Call]:
    raw = message.get("url")
    if not isinstance(raw, str) or not raw.strip():
        raise ShellProtocolError("url must be a non-empty string")
    url = raw.strip()
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ShellProtocolError(
            f"scheme {scheme or '(relative)'!r} is not allowed"
        )
    return [("Page.navigate", {"url": url})]


def translate(message: dict, *, viewport: tuple[int, int]) -> list[Call]:
    """Map one client message to the CDP calls that carry it out, in order.

    A list rather than a single call because a click is a press and a release
    and a keystroke is a down and an up; collapsing either to one event leaves
    the page mid-gesture.

    Raises :class:`ShellProtocolError` for anything the protocol does not
    define. The arguments of an unknown message are never read.
    """

    kind = message.get("t")
    if kind == "click":
        return _click(message, viewport)
    if kind == "scroll":
        return _scroll(message, viewport)
    if kind == "type":
        return _type(message)
    if kind == "key":
        return _key(message)
    if kind == "navigate":
        return _navigate(message)
    if kind == "reload":
        # Nothing from the client: ignoreCache and scriptToEvaluateOnLoad are
        # not the viewer's to set, and the latter is script injection.
        return [("Page.reload", {})]
    raise ShellProtocolError(f"unsupported message type {kind!r}")


class _Cdp(Protocol):
    """The slice of :class:`~surogates.browser.cdp.CdpClient` a session uses."""

    async def call(
        self,
        method: str,
        params: dict | None = ...,
        *,
        session: str | None = ...,
        timeout: float = ...,
    ) -> dict: ...
    def on(self, method: str, handler: Callable[[dict], None]) -> None: ...
    async def targets(self) -> list[dict]: ...
    async def attach_page(self, target_id: str) -> str: ...


class _Client(Protocol):
    """The slice of a Starlette WebSocket a session writes to."""

    async def send_bytes(self, payload: bytes) -> None: ...
    async def send_text(self, payload: str) -> None: ...


class ShellSession:
    """Drives one viewer's connection: CDP in one hand, the client in the other.

    Dependencies are injected rather than dialled, so the whole pump — the
    lease gate, frame ordering, the switch-tab sequence — is testable without a
    browser. Two rules it exists to enforce:

    * Frames always flow; only the command half is gated. A viewer who does not
      hold the lease still watches.
    * The screencast starts at attach time and never after a navigation.
      ``Page.startScreencast`` is refused with "Not attached to an active page"
      while one is in flight.
    """

    def __init__(
        self,
        cdp: _Cdp,
        client: _Client,
        *,
        lease_held: Callable[[], Awaitable[bool]],
        max_message: int = MAX_WS_MESSAGE,
    ) -> None:
        self._cdp = cdp
        self._client = client
        self._lease_held = lease_held
        self._max_message = max_message
        self._session: str | None = None
        self._target: str | None = None
        self._viewport: tuple[int, int] = DEFAULT_VIEWPORT
        # Frames are queued rather than sent from the CDP event handler: the
        # handler is synchronous, and spawning a task per frame would let two
        # sends interleave and paint a stale image.
        self._frames: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._dropped = 0

    async def start(self) -> None:
        pages = await self._cdp.targets()
        if not pages:
            raise RuntimeError("browser has no page target")
        self._cdp.on("Page.screencastFrame", self._on_frame)
        self._cdp.on("Page.frameNavigated", self._on_navigated)
        for event in (
            "Target.targetCreated",
            "Target.targetDestroyed",
            "Target.targetInfoChanged",
        ):
            self._cdp.on(event, self._on_targets_changed)
        # Chrome sends none of those events until discovery is on; without
        # this the tab strip is a snapshot from connect time.
        await self._cdp.call("Target.setDiscoverTargets", {"discover": True})
        self._pump = asyncio.create_task(self._pump_frames())
        await self._attach(pages[0]["targetId"])
        await self._push_tabs()

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None

    async def handle(self, raw: str | bytes) -> None:
        """Act on one client frame. Never raises for bad input — drops it."""

        if len(raw) > self._max_message:
            self._drop("oversized message")
            return
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            self._drop("malformed json")
            return
        if not isinstance(message, dict):
            self._drop("message is not an object")
            return

        kind = message.get("t")
        if kind in COMMAND_TYPES and not await self._lease_held():
            # Dropped, not an error: the lease expiring mid-session is normal,
            # and the viewer keeps watching either way.
            self._drop("no control lease")
            return

        try:
            if kind == "switch_tab":
                await self._switch_tab(message.get("id"))
            elif kind == "close_tab":
                await self._close_tab(message.get("id"))
            elif kind in {"back", "forward"}:
                await self._history(kind)
            else:
                for method, params in translate(message, viewport=self._viewport):
                    await self._cdp.call(method, params, session=self._session)
        except ShellProtocolError as exc:
            self._drop(exc.reason)
        except Exception:  # noqa: BLE001 - one bad command never kills a viewer
            logger.exception("browser shell command failed")

    async def _attach(self, target_id: str) -> None:
        self._target = target_id
        self._session = await self._cdp.attach_page(target_id)
        await self._cdp.call("Page.enable", session=self._session)
        await self._refresh_viewport()
        # Last, and before anything navigates: see the class docstring.
        await self._cdp.call(
            "Page.startScreencast", dict(SCREENCAST_PARAMS), session=self._session
        )

    async def _switch_tab(self, target_id: object) -> None:
        if not isinstance(target_id, str) or not target_id:
            raise ShellProtocolError("switch_tab needs a target id")
        if self._session is not None:
            # Stop and drain before re-attaching, so a frame from the old
            # target cannot arrive after the switch and paint the wrong page.
            await self._cdp.call("Page.stopScreencast", session=self._session)
            self._drain_frames()
        await self._attach(target_id)
        await self._push_tabs()

    async def _close_tab(self, target_id: object) -> None:
        if not isinstance(target_id, str) or not target_id:
            raise ShellProtocolError("close_tab needs a target id")
        pages = await self._cdp.targets()
        if len(pages) <= 1:
            # The last tab is the browser: closing it leaves nothing to
            # stream and nothing for the agent to come back to.
            raise ShellProtocolError("cannot close the last tab")
        survivors = [
            page["targetId"]
            for page in pages
            if page.get("targetId") != target_id
        ]
        if len(survivors) == len(pages):
            raise ShellProtocolError("unknown tab")
        await self._cdp.call("Target.closeTarget", {"targetId": target_id})
        if target_id == self._target:
            # The screencast died with its target; move to a survivor
            # rather than leaving the viewer on a dead stream.
            self._drain_frames()
            await self._attach(survivors[0])
        await self._push_tabs()

    async def _history(self, direction: str) -> None:
        history = await self._cdp.call(
            "Page.getNavigationHistory", session=self._session
        )
        entries = history.get("entries", [])
        index = int(history.get("currentIndex", 0)) + (
            -1 if direction == "back" else 1
        )
        if not 0 <= index < len(entries):
            return
        await self._cdp.call(
            "Page.navigateToHistoryEntry",
            {"entryId": entries[index]["id"]},
            session=self._session,
        )

    async def _refresh_viewport(self) -> None:
        metrics = await self._cdp.call(
            "Page.getLayoutMetrics", session=self._session
        )
        visual = metrics.get("cssVisualViewport") or {}
        width = int(visual.get("clientWidth") or 0)
        height = int(visual.get("clientHeight") or 0)
        if width > 0 and height > 0:
            self._viewport = (width, height)

    async def _push_tabs(self) -> None:
        pages = await self._cdp.targets()
        await self._send({
            "t": "tabs",
            "tabs": [
                {
                    "id": page.get("targetId"),
                    "title": page.get("title", ""),
                    "url": page.get("url", ""),
                    "active": page.get("targetId") == self._target,
                }
                for page in pages
            ],
        })

    def _on_frame(self, params: dict) -> None:
        try:
            payload = base64.b64decode(params["data"])
        except Exception:  # noqa: BLE001 - a malformed frame is not fatal
            return
        self._frames.put_nowait((payload, params.get("sessionId", "")))

    def _on_navigated(self, params: dict) -> None:
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return  # a subframe navigating is not the address bar's business
        asyncio.create_task(self._after_navigation(frame))

    def _on_targets_changed(self, _params: dict) -> None:
        asyncio.create_task(self._push_tabs())

    async def _after_navigation(self, frame: dict) -> None:
        try:
            await self._refresh_viewport()
            await self._send({
                "t": "nav",
                "url": frame.get("url", ""),
                "title": frame.get("name", ""),
            })
        except Exception:  # noqa: BLE001 - background task, never fatal
            logger.exception("browser shell navigation update failed")

    async def _pump_frames(self) -> None:
        while True:
            payload, screencast_session = await self._frames.get()
            try:
                await self._client.send_bytes(payload)
                # An unacked stream stalls by design.
                await self._cdp.call(
                    "Page.screencastFrameAck",
                    {"sessionId": screencast_session},
                    session=self._session,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dropped frame is not fatal
                logger.debug("browser shell frame forward failed", exc_info=True)

    def _drain_frames(self) -> None:
        while not self._frames.empty():
            self._frames.get_nowait()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._client.send_text(json.dumps(message))

    def _drop(self, reason: str) -> None:
        self._dropped += 1
        logger.debug("browser shell dropped a message: %s", reason)
