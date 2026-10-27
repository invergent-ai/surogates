"""What the cloud's browser client and the computer's share: the collector, the page tree, key names."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from surogates.browser.client import (
    SNAPSHOT_COLLECTOR,
    BrowserClientBase,
    ClickTimeoutError,
    KernelBrowserClient,
)

# Two frames, as a snapshot gives them: the page, and an iframe at (100, 200).
FRAMES = {
    "url": "https://example.com/",
    "title": "Example",
    "viewport": {"width": 1280, "height": 800},
    "frames": [
        {"x": 0, "y": 0, "nodes": [
            {"role": "heading", "name": "Results", "x": 0, "y": 0, "width": 200, "height": 20, "depth": 3,
             "children_count": 0, "idx": 0, "text_block": "Results", "heading_level": 2, "backend_node_id": 11},
            {"role": "button", "name": "Search", "x": 10, "y": 30, "width": 80, "height": 20, "depth": 4,
             "children_count": 0, "idx": 1, "text_block": "", "backend_node_id": 12},
        ]},
        {"x": 100, "y": 200, "nodes": [
            {"role": "link", "name": "More", "x": 5, "y": 5, "width": 40, "height": 10, "depth": 2,
             "children_count": 0, "idx": 2, "text_block": "", "backend_node_id": 13},
        ]},
    ],
}


def test_the_cloud_runs_the_shared_collector() -> None:
    assert SNAPSHOT_COLLECTOR.startswith("({selector, base}) => {")
    script = KernelBrowserClient("http://browser")._snapshot_script("#results")
    assert f"const __surogatesCollect = {SNAPSHOT_COLLECTOR};" in script
    assert 'const __surogatesSelector = "#results";' in script


async def test_the_page_tree_and_its_refs_come_from_a_snapshots_frames() -> None:
    client = KernelBrowserClient("http://browser")

    async def execute(code: str, **_: Any) -> dict[str, Any]:
        return FRAMES

    client._playwright_execute = execute  # type: ignore[method-assign]
    try:
        state = await client.get_state()
    finally:
        await client.close()

    assert [(entry["ref"], entry["role"], entry["x"], entry["y"]) for entry in state["tree"]] == [
        ("@e1", "heading", 100, 10), ("@e2", "button", 50, 40), ("@e3", "link", 125, 210),
    ]
    assert client._snapshot_cache["@e2"] == {
        "x": 50, "y": 40, "role": "button", "name": "Search", "backend_node_id": 12, "nth": 0,
    }


def test_keys_are_joined_into_one_chord_in_playwrights_names() -> None:
    base = BrowserClientBase()
    assert base._chord(("ctrl", "Shift", "esc")) == "Control+Shift+Escape"
    with pytest.raises(ValueError):
        base._chord(())


def test_a_snapshot_detaches_the_cdp_session_it_opens() -> None:
    assert "__cdp.detach()" in KernelBrowserClient("http://browser")._snapshot_script(None)


def test_a_ref_click_is_bounded_answers_dialogs_and_detaches() -> None:
    code = KernelBrowserClient("http://browser")._build_ref_click_js(
        {"ref": "@e1", "role": "button", "name": "Go", "backend_node_id": 1, "nth": 0}, "left", 1,
    )
    assert "cdp = await page.context().newCDPSession(page);" in code
    assert "const cdp =" not in code  # the wrapper's own binding is the one detached
    assert "cdp.detach()" in code
    assert "page.on('dialog', __onDialog)" in code and "page.off('dialog', __onDialog)" in code
    assert "reject(new Error('click_timeout'))" in code


@pytest.mark.parametrize("failure", [
    RuntimeError("Error: click_timeout"),
    httpx.ReadTimeout("read timed out"),
])
async def test_a_stalled_click_is_a_click_timeout(failure: Exception) -> None:
    client = KernelBrowserClient("http://browser")
    client._snapshot_cache["@e1"] = {"role": "button", "name": "Go", "backend_node_id": 1, "nth": 0}
    budgets: list[int] = []

    async def execute(code: str, *, timeout_sec: int = 60) -> Any:
        budgets.append(timeout_sec)
        raise failure

    client._playwright_execute = execute  # type: ignore[method-assign]
    try:
        with pytest.raises(ClickTimeoutError):
            await client.click_ref("@e1")
        with pytest.raises(ClickTimeoutError):
            await client.click_at(5, 5)
    finally:
        await client.close()
    assert budgets and all(budget < 60 for budget in budgets)


async def test_a_click_returns_the_dialogs_it_answered() -> None:
    client = KernelBrowserClient("http://browser")
    dialog = {"type": "alert", "message": "Saved"}

    async def execute(code: str, **_: Any) -> Any:
        return {"dialogs": [dialog]}

    client._playwright_execute = execute  # type: ignore[method-assign]
    try:
        assert await client.click_at(5, 5) == [dialog]
    finally:
        await client.close()


async def test_other_click_failures_are_not_mistaken_for_a_timeout() -> None:
    client = KernelBrowserClient("http://browser")

    async def execute(code: str, **_: Any) -> Any:
        raise RuntimeError("ref click blocked: covered by <div>")

    client._playwright_execute = execute  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="covered"):
            await client.click_at(5, 5)
    finally:
        await client.close()
