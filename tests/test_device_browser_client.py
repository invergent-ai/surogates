"""The browser tools' client for a session on the user's computer: raw operations, logic here."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from surogates.browser.client import KernelBrowserClient
from surogates.devices import browser as device_browser
from surogates.devices.browser import (
    LOCATE,
    NO_BROWSER,
    SNAPSHOT,
    BrowserRefusal,
    DeviceBrowserClient,
    snapshot_cache,
)
from surogates.devices.workspace import DeviceOperationError
from tests.test_browser_client_base import FRAMES


class ScriptedRunner:
    """The computer's half, scripted: each operation asked for, and what it answers."""

    def __init__(self, *outcomes: dict[str, Any]) -> None:
        self.outcomes = list(outcomes)
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def run(self, kind: str, args: dict[str, Any], payload: bytes | None = None) -> dict[str, Any]:
        # Through JSON, as the link carries them.
        self.asked.append((kind, json.loads(json.dumps(args))))
        return self.outcomes.pop(0)


async def test_the_page_tree_is_the_cloud_clients_from_the_same_frames() -> None:
    runner = ScriptedRunner({"ok": FRAMES})
    kernel = KernelBrowserClient("http://browser")

    async def execute(code: str, **_: Any) -> dict[str, Any]:
        return FRAMES

    kernel._playwright_execute = execute  # type: ignore[method-assign]
    try:
        from_cloud = await kernel.get_state()
    finally:
        await kernel.close()
    device = DeviceBrowserClient(runner)

    assert await device.get_state() == from_cloud
    assert runner.asked == [("browser.observe", {"script": SNAPSHOT, "params": {"selector": None}})]
    assert device._snapshot_cache == kernel._snapshot_cache


async def test_a_ref_is_found_again_and_clicked_where_it_is_now() -> None:
    runner = ScriptedRunner({"ok": FRAMES}, {"ok": {"x": 55, "y": 41}}, {"ok": {}})
    client = DeviceBrowserClient(runner)
    await client.get_state()

    await client.click_ref("@e2", num_clicks=2)

    assert runner.asked[1:] == [
        ("browser.observe", {"script": LOCATE, "params": {"backend_node_id": 12, "role": "button", "name": "Search", "nth": 0}}),
        ("browser.mouse", {"action": "click", "x": 55, "y": 41, "button": "left", "clicks": 2}),
    ]


async def test_a_ref_that_is_gone_hidden_or_covered_is_not_clicked() -> None:
    for found, said in [
        ({"missing": "gone"}, "Unknown ref @e2; the element is gone"),
        ({"missing": "hidden"}, "ref element not visible"),
        ({"missing": "unmeasurable"}, "ref element not measurable"),
        ({"covered": "div#consent.banner"}, "ref click blocked: covered by <div#consent.banner>"),
    ]:
        runner = ScriptedRunner({"ok": FRAMES}, {"ok": found})
        client = DeviceBrowserClient(runner)
        await client.get_state()
        with pytest.raises(RuntimeError, match=said):
            await client.click_ref("@e2")
        assert [kind for kind, _ in runner.asked] == ["browser.observe", "browser.observe"]


async def test_an_unknown_ref_asks_the_computer_nothing() -> None:
    runner = ScriptedRunner()
    with pytest.raises(KeyError):
        await DeviceBrowserClient(runner).click_ref("@e9")
    assert runner.asked == []


async def test_typing_into_a_ref_and_a_drag_are_one_operation_each() -> None:
    runner = ScriptedRunner({"ok": FRAMES}, {"ok": {"x": 50, "y": 40}}, {"ok": {}}, {"ok": {}})
    client = DeviceBrowserClient(runner)
    await client.get_state()

    await client.type_into_ref("@e2", "héllo", delay_ms=5)
    await client.drag([(1, 2), (3, 4), (5, 6)])

    assert runner.asked[2:] == [
        ("browser.keyboard", {"action": "type", "text": "héllo", "at": {"x": 50, "y": 40}, "delay": 5}),
        ("browser.mouse", {"action": "drag", "path": [[1, 2], [3, 4], [5, 6]], "button": "left"}),
    ]


async def test_keys_go_as_one_chord_in_playwrights_names_and_a_scroll_says_where_it_ended() -> None:
    position = {"scroll_x": 0, "scroll_y": 300, "page_height": 4000, "viewport_height": 800}
    runner = ScriptedRunner({"ok": {}}, {"ok": {**position, "notices": []}})
    client = DeviceBrowserClient(runner)

    await client.press_key("ctrl", "a")
    assert await client.scroll_at(10, 20, delta_y=300) == position

    assert runner.asked == [
        ("browser.keyboard", {"action": "press", "keys": "Control+a", "delay": 0}),
        ("browser.mouse", {"action": "wheel", "x": 10, "y": 20, "delta_x": 0, "delta_y": 300}),
    ]


async def test_a_navigation_forgets_the_refs_and_keeps_what_the_page_did_unseen() -> None:
    runner = ScriptedRunner({"ok": FRAMES}, {"ok": {
        "url": "https://example.com/next", "title": "Next", "opened": False,
        "notices": ["The page started a download (report.pdf)."],
    }})
    client = DeviceBrowserClient(runner)
    await client.get_state()

    assert await client.navigate("https://example.com/next") == {"url": "https://example.com/next", "title": "Next"}
    assert client._snapshot_cache == {}
    assert client.notices == ["The page started a download (report.pdf)."]
    assert runner.asked[-1] == ("browser.navigate", {"url": "https://example.com/next", "wait_until": "load"})


@pytest.mark.parametrize(("act", "answer", "said"), [
    (lambda client: client.navigate("https://example.com/"), "a page", "The computer returned an invalid navigation"),
    (lambda client: client.navigate("https://example.com/"), {"url": 1, "title": "T"}, "The computer returned an invalid navigation"),
    (lambda client: client.click_ref("@e2"), {}, "The computer returned an invalid place"),
    (lambda client: client.click_ref("@e2"), {"x": "50", "y": 40}, "The computer returned an invalid place"),
    (lambda client: client.scroll_at(1, 2, delta_y=3), {"scroll_x": 0, "scroll_y": "a", "page_height": 1, "viewport_height": 1},
     "The computer returned an invalid scroll position"),
    (lambda client: client.scroll_at(1, 2, delta_y=3), [], "The computer returned an invalid scroll position"),
])
async def test_an_answer_of_the_wrong_shape_is_said_as_the_computers_failure(act, answer, said) -> None:
    runner = ScriptedRunner({"ok": FRAMES}, {"ok": answer})
    client = DeviceBrowserClient(runner)
    await client.get_state()

    with pytest.raises(DeviceOperationError, match=said):
        await act(client)


async def test_an_operation_too_large_for_the_link_says_it_is_the_browsers_and_asks_nothing() -> None:
    runner = ScriptedRunner()
    with pytest.raises(DeviceOperationError) as large:
        await DeviceBrowserClient(runner).evaluate("x" * (2 * 1024 * 1024))
    assert str(large.value) == "Too large for one operation in the browser on this computer (over 1.5 MiB)"
    assert runner.asked == []


async def test_a_scripts_value_comes_back_whatever_its_shape() -> None:
    # A page's own data, shaped as the link's framing: a value all the same.
    returned = {"transfer": {"size": 5, "sha256": "a" * 64}}
    runner = ScriptedRunner({"ok": {"value": returned}}, {"ok": {"value": None}})
    client = DeviceBrowserClient(runner)

    assert await client.evaluate("return await (await fetch('/api/transfers/1')).json();") == returned
    assert await client.evaluate("document.title = 'x';") is None
    assert runner.asked[0] == ("browser.evaluate", {"code": "return await (await fetch('/api/transfers/1')).json();"})


async def test_a_screenshot_comes_inline_or_as_a_transfer_and_labels_the_refs_it_was_asked_to() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    runner = ScriptedRunner(
        {"ok": base64.b64encode(png).decode("ascii")},
        {"ok": FRAMES},
        # A transfer's data, as the journal's runner hands it back once fetched and checked.
        {"ok": png},
    )
    client = DeviceBrowserClient(runner)

    assert await client.screenshot(region={"x": 1, "y": 2, "width": 3, "height": 4}) == {"png_bytes": png}
    shot = await client.screenshot(annotate=True)

    assert shot["png_bytes"] == png
    assert [note["ref"] for note in shot["annotations"]] == ["@e1", "@e2", "@e3"]
    assert runner.asked[0] == ("browser.screenshot", {"clip": {"x": 1, "y": 2, "width": 3, "height": 4}, "labels": []})
    assert runner.asked[2] == ("browser.screenshot", {"clip": None, "labels": [
        {"label": 1, "x": 100, "y": 10}, {"label": 2, "x": 50, "y": 40}, {"label": 3, "x": 125, "y": 210},
    ]})


async def test_screenshot_data_that_is_not_strict_base64_is_refused() -> None:
    with pytest.raises(DeviceOperationError, match="invalid screenshot"):
        await DeviceBrowserClient(ScriptedRunner({"ok": "iVBO-_"})).screenshot()


async def test_what_the_computer_refuses_is_the_tools_result_and_its_failures_are_the_pages() -> None:
    cases = [
        ({"type": "denied", "message": "The user did not let the agent use the browser on this computer"},
         {"error": "denied", "detail": "The user did not let the agent use the browser on this computer"}),
        ({"type": "no_browser", "message": "none here"}, {"error": "no_browser", "detail": NO_BROWSER}),
        ({"type": "unsupported", "message": "unknown kind"}, {"error": "unsupported", "detail": device_browser.OLD_APP}),
    ]
    for error, result in cases:
        with pytest.raises(BrowserRefusal) as refused:
            await DeviceBrowserClient(ScriptedRunner({"error": error})).navigate("https://example.com")
        assert json.loads(refused.value.result) == result
    with pytest.raises(RuntimeError, match="net::ERR_NAME_NOT_RESOLVED"):
        await DeviceBrowserClient(ScriptedRunner(
            {"error": {"type": "browser", "message": "net::ERR_NAME_NOT_RESOLVED"}},
        )).navigate("https://nowhere.example")


async def test_cloud_profiles_are_not_available_on_the_computer() -> None:
    client = DeviceBrowserClient(ScriptedRunner())
    with pytest.raises(NotImplementedError):
        await client.storage_state()


async def test_each_session_keeps_its_own_refs_and_only_the_newest_sessions_are_kept() -> None:
    first = snapshot_cache("session-a")
    first["@e1"] = {"x": 1}
    assert snapshot_cache("session-a") is first
    assert snapshot_cache("session-b") == {}
    for n in range(device_browser._CACHED_SESSIONS):
        snapshot_cache(f"other-{n}")
    assert snapshot_cache("session-a") == {}
