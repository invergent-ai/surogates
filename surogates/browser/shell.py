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

import math
from urllib.parse import urlparse

# Message types that act on the page, and so require the control lease.
# ``switch_tab`` is deliberately absent: it changes what a viewer watches, not
# the page, so a viewer without the lease may still look at another tab.
COMMAND_TYPES: frozenset[str] = frozenset({
    "click", "scroll", "type", "key",
    "navigate", "back", "forward", "reload",
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
