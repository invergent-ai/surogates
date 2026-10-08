"""What the cloud's browser client and the computer's share: the collector, the page tree, key names."""

from __future__ import annotations

from typing import Any

import pytest

from surogates.browser.client import (
    SNAPSHOT_COLLECTOR,
    BrowserClientBase,
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
