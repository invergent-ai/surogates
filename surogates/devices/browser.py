"""The browser on the user's computer, reached through device operations.

The worker keeps the logic, as it does for files: the ``@eN`` refs, key names and
the page tree are surogates.browser.client's.  The computer runs only these raw
operations, in the tab of the calling session, never code from the server but a
page's own JavaScript (browser.evaluate), which runs in the page:

  kind                 args                                            ok value
  browser.navigate     url (http: or https: only),                     {url, title, opened, notices}
                       wait_until (load | domcontentloaded |
                       networkidle)
  browser.observe      script (an id below), params (its own)          what the script gives
  browser.evaluate     code (a function body, run in the page)         {value}: its return value, as JSON
  browser.mouse        action, x, y, button, clicks:                   {notices}; a wheel also gives
                       click | down | up | move;                       {scroll_x, scroll_y,
                       wheel, with delta_x, delta_y;                    page_height, viewport_height}
                       drag, with path [[x, y], ...]
  browser.keyboard     action: type, with text and at {x, y} or        {notices}
                       null (a click there first); press, with keys
                       (one chord, in Playwright's names); delay (ms)
  browser.screenshot   clip {x, y, width, height} or null,             PNG data (base64), or
                       labels [{label, x, y}] drawn over the page       {"transfer": {size, sha256}}
                       for the shot                                     over MAX_PAYLOAD_BYTES
  browser.close                                                        {closed}

The scripts browser.observe takes, each a page function shipped with the app:

  snapshot@1  selector (str or null)    {url, title, viewport, frames: [{x, y, nodes}]}: each
                                        frame's nodes as surogates/browser/observe/snapshot.js
                                        collects them, with each node's backend_node_id
  locate@1    backend_node_id (int or   {x, y}, the centre to click once scrolled into view;
              null), role, name, nth    or {missing: "gone" | "hidden" | "unmeasurable"};
                                        or {covered: "<tag#id.class>"}

notices are what the page did that the agent could not see happen, such as a
file it asked for.  opened is whether the navigation is the first to
answer in the session's tab: its first tab, or one after its last closed, whether
this navigation made the tab or an earlier operation did.  The session's browser pane
hears of it (browser.provisioned), of a close that closed one (browser.destroyed) and
of a computer with no supported browser (browser.unavailable), each with
``computer: true`` and the session whose tab it is.  A sub-agent's are written to its
root chat's log too: the chat's pane shows its browser while a tab of its own or of
a sub-agent's is open.  Errors:

  {"type": "browser", "message"}     the page or the browser failed: a RuntimeError,
                                     which the handlers report as their own failure
  {"type": "denied", "message"}      the computer's user did not allow it
  {"type": "no_browser", "message"}  no supported browser on this computer
  {"type": "unsupported", ...}       an app that has no browser yet
  {"type": "revoked", "message"}     the computer's access ended
  {"type": "paused_by_user", ...}    its user took the browser over, from one chat and for
                                     every chat of the agent's there, until they hand it
                                     back in the desktop's own confirmation
  any other type                     DeviceOperationError(message), which the handlers
                                     report as their own failure too

Every args object and every outcome fits in one link frame, as in
surogates.devices.workspace; a screenshot over MAX_PAYLOAD_BYTES is a transfer,
as a read's data is (RESULT_TRANSFERS).  A script's value is always under
``value``: whatever a page returns, the link never reads it as a transfer.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Self
from uuid import UUID

from surogates.browser.client import BrowserClientBase
from surogates.browser.control import paused_by_user_result
from surogates.devices.binding import is_binding_root
from surogates.devices.workspace import (
    MAX_MESSAGE_CHARS,
    DeviceOperationError,
    OperationRunner,
)
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

SNAPSHOT = "snapshot@1"
LOCATE = "locate@1"

NO_BROWSER = (
    "No supported browser on this computer. Install Google Chrome, Microsoft Edge, Brave or "
    "Vivaldi, or pick one in Settings → Browser. The Snap build of Chromium is not supported."
)
OLD_APP = "The Surogate app on this computer cannot drive a browser yet. Update it, then try again."
# As surogates.devices.workspace.TOO_LARGE says it of a folder's.
TOO_LARGE = "Too large for one operation in the browser on this computer (over 1.5 MiB)"


class BrowserRefusal(Exception):
    """The computer would not do this.  Its result is the tool's result as it stands.

    Not a RuntimeError: the handlers report one of those as the page failing.
    """

    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result
        self.error = json.loads(result).get("error")


async def tell_pane(
    session_store: Any, session_id: Any, event: EventType, session_config: dict[str, Any] | None = None,
) -> None:
    """Tell the session's browser pane of its browser on the user's computer.

    A sub-agent's tab is in its chat's browser: its root chat's log is written the same event, which
    names the sub-agent, as a sub-agent's artifacts reach its parent's thread
    (surogates.tools.builtin.delegate).  The chat's pane counts a tab for each session it is told of.

    The browser call is answered whether or not a pane hears: what it did on the computer is done.
    """
    if session_store is None or session_id is None:
        return
    logs = [session_id]
    if not is_binding_root(session_id, session_config):
        # As the server stamped it when the session was made under its chat, never from tool input.
        logs.append((session_config or {})["sandbox_root_session_id"])
    for log in logs:
        try:
            await session_store.emit_event(UUID(str(log)), event, {"session_id": str(session_id), "computer": True})
        except Exception:
            logger.warning(
                "Could not tell the browser pane of session %s of %s of session %s",
                log, event.value, session_id, exc_info=True,
            )


def of_a_sub_agent(event: Any) -> bool:
    """Whether a browser event in a session's log names another session: a sub-agent's tab, written to
    its root chat's log for the chat's pane."""
    named = event.data.get("session_id")
    return named is not None and str(named) != str(event.session_id)


def answering_refusals(handler: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """A browser tool's handler that answers the computer's refusals as its result.

    A computer with no supported browser is said in the session's browser pane too.
    """

    @functools.wraps(handler)
    async def answered(arguments: dict[str, Any], **kwargs: Any) -> str:
        try:
            return await handler(arguments, **kwargs)
        except BrowserRefusal as refusal:
            if refusal.error == "no_browser":
                await tell_pane(
                    kwargs.get("session_store"), kwargs.get("session_id"), EventType.BROWSER_UNAVAILABLE,
                    kwargs.get("session_config"),
                )
            return refusal.result

    return answered


@dataclass(frozen=True)
class DeviceEndpoint:
    """A session on the user's computer reaches its browser through its tool call's runner."""

    runner: OperationRunner


# ponytail: one ref cache per calling session in this worker, the newest
# _CACHED_SESSIONS kept; a session whose cache went asks for its refs again.
_CACHED_SESSIONS = 256
_caches: OrderedDict[str, dict[str, dict[str, Any]]] = OrderedDict()


def snapshot_cache(session_id: str) -> dict[str, dict[str, Any]]:
    """The ``@eN`` refs of *session_id*'s tab on the computer, as its last snapshot made them."""
    cache = _caches.pop(session_id, None)
    if cache is None:
        cache = {}
    _caches[session_id] = cache
    while len(_caches) > _CACHED_SESSIONS:
        _caches.popitem(last=False)
    return cache


def forget_snapshot_cache(session_id: str) -> None:
    _caches.pop(session_id, None)


class DeviceBrowserClient(BrowserClientBase):
    """The browser tools' client for a session on the user's computer.

    The same public methods as KernelBrowserClient for browser interaction.
    Cloud profiles and ``storage_state`` are not available: the computer keeps
    its own profile.
    """

    def __init__(
        self,
        runner: OperationRunner,
        *,
        snapshot_cache: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(snapshot_cache=snapshot_cache)
        self._runner = runner
        # What the page did that the agent could not see happen, in the order the computer said.
        self.notices: list[str] = []
        # Whether the last navigation opened the session's tab on the computer.
        self.opened = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def close(self) -> None:
        return None

    async def navigate(self, url: str, *, wait_until: str = "load") -> dict[str, Any]:
        value = await self._call("browser.navigate", url=url, wait_until=wait_until)
        self._invalidate_snapshot_cache()
        if not isinstance(value, dict) or not all(isinstance(value.get(key, ""), str) for key in ("url", "title")):
            raise DeviceOperationError("The computer returned an invalid navigation")
        self.opened = value.get("opened") is True
        return {"url": value.get("url", url), "title": value.get("title", "")}

    async def evaluate(self, code: str) -> Any:
        value = await self._call("browser.evaluate", code=code)
        self._invalidate_snapshot_cache()
        if not isinstance(value, dict):
            raise DeviceOperationError("The computer returned an invalid result for the script")
        return value.get("value")

    async def get_state(
        self,
        *,
        interactive_only: bool = False,
        max_depth: int | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        raw = await self._call("browser.observe", script=SNAPSHOT, params={"selector": selector})
        if not isinstance(raw, dict):
            raise DeviceOperationError("The computer returned an invalid snapshot")
        return self._state_from(raw, interactive_only=interactive_only, max_depth=max_depth)

    async def click_at(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        click_type: str = "click",
        num_clicks: int = 1,
    ) -> None:
        if click_type not in ("click", "down", "up"):
            raise ValueError(f"unsupported click_type: {click_type}")
        await self._call("browser.mouse", action=click_type, x=int(x), y=int(y), button=button, clicks=int(num_clicks))

    async def click_ref(self, ref: str, **kwargs: Any) -> None:
        x, y = await self._locate(ref)
        await self.click_at(x, y, button=kwargs.get("button", "left"), num_clicks=kwargs.get("num_clicks", 1))

    async def type_text(self, text: str, *, delay_ms: int = 0) -> None:
        await self._call("browser.keyboard", action="type", text=text, at=None, delay=int(delay_ms))

    async def type_into_ref(self, ref: str, text: str, **kwargs: Any) -> None:
        # One operation, so a chat that asks every time asks once: the click to focus, then the typing.
        x, y = await self._locate(ref)
        await self._call(
            "browser.keyboard", action="type", text=text, at={"x": x, "y": y}, delay=int(kwargs.get("delay_ms", 0)),
        )

    async def press_key(self, *keys: str, duration_ms: int = 0) -> None:
        await self._call("browser.keyboard", action="press", keys=self._chord(keys), delay=int(duration_ms))

    async def scroll_at(self, x: int, y: int, *, delta_x: int = 0, delta_y: int = 0) -> dict[str, Any]:
        value = await self._call(
            "browser.mouse", action="wheel", x=int(x), y=int(y), delta_x=int(delta_x), delta_y=int(delta_y),
        )
        keys = ("scroll_x", "scroll_y", "page_height", "viewport_height")
        if not isinstance(value, dict) or not all(_whole(value.get(key)) for key in keys):
            raise DeviceOperationError("The computer returned an invalid scroll position")
        return {key: value[key] for key in keys}

    async def drag(self, path: list[tuple[int, int]], *, button: str = "left") -> None:
        if len(path) < 2:
            raise ValueError("drag path must contain at least two points")
        # One operation, so a chat that asks every time asks once for the whole drag.
        await self._call("browser.mouse", action="drag", path=[[int(x), int(y)] for x, y in path], button=button)

    async def screenshot(
        self,
        *,
        region: dict[str, int] | None = None,
        annotate: bool = False,
        save_path: str | None = None,
    ) -> dict[str, Any]:
        """A PNG of the session's tab.  *save_path* is the cloud's: the worker saves this one."""
        annotations: list[dict[str, Any]] | None = None
        labels: list[dict[str, int]] = []
        if annotate:
            if not self._snapshot_cache:
                await self.get_state(interactive_only=True)
            annotations = self._build_annotations()
            labels = [
                {
                    "label": int(note["label"]),
                    "x": int(self._snapshot_cache[note["ref"]]["x"]),
                    "y": int(self._snapshot_cache[note["ref"]]["y"]),
                }
                for note in annotations
            ]
        clip = None if region is None else {side: int(region[side]) for side in ("x", "y", "width", "height")}
        data = await self._call("browser.screenshot", clip=clip, labels=labels)
        result: dict[str, Any] = {"png_bytes": _png(data)}
        if annotations is not None:
            result["annotations"] = annotations
        return result

    async def close_tab(self) -> bool:
        """Close this session's tab and the popups it opened; whether there was one."""
        value = await self._call("browser.close")
        return bool(value.get("closed")) if isinstance(value, dict) else False

    async def storage_state(self) -> dict[str, Any]:
        raise NotImplementedError("Browser profiles are not available for sessions on a local folder")

    async def apply_storage_state(self, state: dict[str, Any]) -> None:
        raise NotImplementedError("Browser profiles are not available for sessions on a local folder")

    async def _locate(self, ref: str) -> tuple[int, int]:
        """Where *ref* is now, found again as the cloud's ref click finds it, or its error."""
        entry = self._resolve_ref(ref)
        value = await self._call("browser.observe", script=LOCATE, params={
            "backend_node_id": entry.get("backend_node_id"),
            "role": str(entry.get("role", "")),
            "name": str(entry.get("name", "")),
            "nth": int(entry.get("nth", 0)),
        })
        if not isinstance(value, dict) or not (
            "missing" in value or "covered" in value or (_whole(value.get("x")) and _whole(value.get("y")))
        ):
            raise DeviceOperationError("The computer returned an invalid place")
        missing = value.get("missing")
        if missing == "gone":
            raise RuntimeError(f"Unknown ref {ref}; the element is gone — call browser_get_state to refresh refs")
        if missing == "hidden":
            raise RuntimeError("ref element not visible; call browser_get_state to refresh refs")
        if missing is not None:
            raise RuntimeError("ref element not measurable; call browser_get_state to refresh refs")
        if value.get("covered"):
            raise RuntimeError(
                f"ref click blocked: covered by <{value['covered']}>. Dismiss that element, then "
                "browser_get_state and retry."
            )
        return int(value["x"]), int(value["y"])

    async def _call(self, kind: str, **args: Any) -> Any:
        if len(json.dumps(args)) > MAX_MESSAGE_CHARS:
            raise DeviceOperationError(TOO_LARGE)
        outcome = await self._runner.run(kind, args)
        error = outcome.get("error")
        if isinstance(error, dict):
            refused = error.get("type")
            message = str(error.get("message", ""))
            if refused == "denied":
                raise BrowserRefusal(json.dumps({"error": "denied", "detail": message}))
            if refused == "no_browser":
                raise BrowserRefusal(json.dumps({"error": "no_browser", "detail": NO_BROWSER}))
            if refused == "unsupported":
                raise BrowserRefusal(json.dumps({"error": "unsupported", "detail": OLD_APP}))
            if refused == "revoked":
                raise BrowserRefusal(json.dumps({"error": "revoked", "detail": message}))
            if refused == "paused_by_user":
                raise BrowserRefusal(paused_by_user_result())
            raise DeviceOperationError(message)
        if "ok" not in outcome:
            raise DeviceOperationError(f"The computer returned no result for {kind}")
        value = outcome["ok"]
        if isinstance(value, dict) and isinstance(value.get("notices"), list):
            self.notices.extend(str(notice) for notice in value["notices"])
        return value


def _whole(value: Any) -> bool:
    """Whether *value* is a whole number, as the page's coordinates are: not a bool, not text."""
    return isinstance(value, int) and not isinstance(value, bool)


def _png(data: Any) -> bytes:
    """A screenshot's bytes: a transfer's, which the runner fetched and checked, or strict base64."""
    if isinstance(data, bytes):
        return data
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise DeviceOperationError("The computer returned an invalid screenshot") from None
