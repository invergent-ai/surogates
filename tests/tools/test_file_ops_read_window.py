"""Read-window rendering: overflow must yield content plus a resume offset.

An over-budget read used to return ``{"error": ...}`` with no content, which
costs a full round trip and tells the model nothing about where to continue.
"""

from __future__ import annotations

import io
import json
import random
from pathlib import Path

import pytest

from surogates.tools.builtin.file_ops import (
    _read_file_handler,
    _render_read_window,
    clear_read_tracker,
)
from surogates.tools.workspace_io import LocalWorkspaceIO

# As read_file picks a codec from a file's head.
BOMS = [
    (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8-sig"),
]


@pytest.mark.asyncio
async def test_oversized_read_returns_content_not_error(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "surogates.tools.builtin.file_ops.get_max_bytes", lambda: 40,
    )
    src = tmp_path / "big.txt"
    src.write_text("".join(f"line {i}\n" for i in range(1, 51)), encoding="utf-8")

    result = json.loads(await _read_file_handler({"path": str(src)}))

    assert "error" not in result, result
    assert result["content"].startswith("line 1\n")
    assert result["truncated"] is True
    assert result["next_offset"] == result["lines_shown"] + 1
    assert "offset=" in result["_hint"]


def whole_file_read(data: bytes, offset: int, limit: int) -> dict:
    """What read_file showed before it read pages: the whole file decoded, then its window rendered."""
    encoding = next((name for bom, name in BOMS if data.startswith(bom)), "utf-8")
    lines = io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors="replace").readlines()
    window = lines[offset - 1:min(offset - 1 + limit, len(lines))]
    content, shown, next_offset = _render_read_window(window, offset, len(lines))
    return {"content": content, "total_lines": len(lines), "lines_shown": shown, "next_offset": next_offset}


@pytest.mark.asyncio
async def test_a_page_reads_as_the_whole_file_did(tmp_path: Path, monkeypatch) -> None:
    # The cloud's read_file is byte for byte what it was: every codec, CR LFs, invalid bytes, small budgets.
    rng = random.Random(11)
    path = tmp_path / "f.txt"
    # U+FEFF too: a utf-8-sig page decoded as utf-8-sig would drop one.
    alphabet = ["a", "bb", "é", "€", "😀", "\n", "\r\n", "\r", "x" * 7, "中文", "上", "不", "\ufeff"]
    for _ in range(1500):
        max_chars = rng.randint(1, 14)
        monkeypatch.setattr("surogates.tools.builtin.file_ops.get_max_bytes", lambda m=max_chars: m)
        bom, encoding = rng.choice([*BOMS, (b"", "utf-8")])
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        body = text.encode("utf-8" if encoding == "utf-8-sig" else encoding)
        if rng.random() < 0.3:
            body = bytes(rng.choice([byte, 0x80, 0xFF, 0xD8]) for byte in body)
        path.write_bytes(bom + body)
        offset, limit = rng.randint(1, 8), rng.randint(-8, 6)
        clear_read_tracker()
        got = json.loads(await _read_file_handler(
            {"path": str(path), "offset": offset, "limit": limit}, workspace_io=LocalWorkspaceIO(str(tmp_path)),
        ))
        want = whole_file_read(bom + body, offset, limit)
        assert {key: got.get(key) for key in want} == want, (bom + body, offset, limit, max_chars)


@pytest.mark.asyncio
async def test_a_budget_over_a_device_page_still_pages_as_the_whole_file_did(tmp_path: Path, monkeypatch) -> None:
    # The 1 MiB clamp is the device's: at 300 000 characters of 4-byte text, a cloud page holds more than 1 MiB.
    monkeypatch.setattr("surogates.tools.builtin.file_ops.get_max_bytes", lambda: 300_000)
    path = tmp_path / "f.txt"
    path.write_bytes(("😀" * 300 + "\n").encode() * 2000)
    clear_read_tracker()
    got = json.loads(await _read_file_handler({"path": str(path)}, workspace_io=LocalWorkspaceIO(str(tmp_path))))
    want = whole_file_read(path.read_bytes(), 1, 2000)
    assert {key: got.get(key) for key in want} == want
