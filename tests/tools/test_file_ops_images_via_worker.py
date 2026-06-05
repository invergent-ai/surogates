"""Unit tests for the worker's read_file→vision_analyze pre-dispatch branch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def clear_image_cache():
    """Each test starts with a clean image cache to avoid cross-test bleed."""
    from surogates.harness.image_read import _CACHE

    _CACHE.clear()
    yield
    _CACHE.clear()


@pytest.mark.asyncio
async def test_read_png_calls_vision_and_renders_as_read_file(
    tmp_path: Path,
) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n placeholder")

    dispatched = []

    async def fake_dispatch(tool_name: str, args, **kwargs) -> str:
        dispatched.append((tool_name, args))
        return json.dumps({
            "analysis": "A bar chart with three columns.",
            "model": "gpt-x",
        })

    result_json = await handle_image_read(
        path=str(img),
        arguments={"path": str(img)},
        dispatch=fake_dispatch,
        kwargs={"workspace_path": str(tmp_path)},
    )
    result = json.loads(result_json)
    assert "error" not in result, result
    assert "# Image: chart.png" in result["content"]
    assert "A bar chart with three columns." in result["content"]
    assert result["path"] == str(img)
    # Underlying call shape goes to vision_analyze with image=path.
    assert dispatched == [("vision_analyze", {"image": str(img)})]


@pytest.mark.asyncio
async def test_read_image_caches_analysis(tmp_path: Path) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    calls = {"n": 0}

    async def counting_dispatch(tool_name: str, args, **kwargs) -> str:
        calls["n"] += 1
        return json.dumps({"analysis": "described"})

    await handle_image_read(
        str(img), {"path": str(img)}, counting_dispatch, {},
    )
    await handle_image_read(
        str(img), {"path": str(img), "offset": 1, "limit": 100},
        counting_dispatch, {},
    )
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_read_image_no_vision_configured(tmp_path: Path) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    async def err_dispatch(tool_name: str, args, **kwargs) -> str:
        return json.dumps({
            "error": "vision_analyze is not available: no vision LLM configured",
        })

    result_json = await handle_image_read(
        str(img), {"path": str(img)}, err_dispatch, {},
    )
    result = json.loads(result_json)
    assert "error" in result
    assert "vision" in result["error"].lower()


@pytest.mark.asyncio
async def test_read_image_pagination_via_offset_limit(tmp_path: Path) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    analysis = "\n".join(f"line {i}" for i in range(1, 21))

    async def dispatch(tool_name: str, args, **kwargs) -> str:
        return json.dumps({"analysis": analysis})

    result_json = await handle_image_read(
        str(img), {"path": str(img), "offset": 5, "limit": 3}, dispatch, {},
    )
    result = json.loads(result_json)
    assert "error" not in result, result
    # Header on line 1, blank on 2, body starts at line 3.
    # offset=5 → start at body line 3 ("line 3"), limit=3 → 3 lines.
    assert "5|line 3" in result["content"]
    assert "7|line 5" in result["content"]
    assert "8|line 6" not in result["content"]
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_read_image_handles_non_json_dispatch(tmp_path: Path) -> None:
    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    async def garbage_dispatch(tool_name: str, args, **kwargs) -> str:
        return "this is not JSON at all"

    result_json = await handle_image_read(
        str(img), {"path": str(img)}, garbage_dispatch, {},
    )
    result = json.loads(result_json)
    assert "error" in result
    assert "non-json" in result["error"].lower()


@pytest.mark.asyncio
async def test_read_image_cache_can_be_disabled_via_env(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("READ_IMAGE_CACHE_DISABLED", "1")

    from surogates.harness.image_read import handle_image_read

    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG placeholder")

    calls = {"n": 0}

    async def counting(tool_name: str, args, **kwargs) -> str:
        calls["n"] += 1
        return json.dumps({"analysis": "described"})

    await handle_image_read(str(img), {"path": str(img)}, counting, {})
    await handle_image_read(str(img), {"path": str(img)}, counting, {})
    # With cache disabled, the second call must re-dispatch.
    assert calls["n"] == 2
