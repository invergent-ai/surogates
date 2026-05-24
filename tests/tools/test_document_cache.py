"""Tests for the file-backed document cache."""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.fixture
def cache(tmp_path: Path):
    from surogates.tools.utils.document_cache import DocumentCache

    return DocumentCache(
        root=tmp_path / "cache",
        max_entries=3,
        max_entry_bytes=1024,
    )


@pytest.mark.asyncio
async def test_miss_then_hit_does_not_reparse(cache, tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"original")

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return f"markdown for {path.name}"

    md1 = await cache.get_or_parse(src, parse)
    md2 = await cache.get_or_parse(src, parse)
    assert md1 == "markdown for doc.pdf"
    assert md2 == md1
    assert calls["n"] == 1, "second call should be a cache hit"


@pytest.mark.asyncio
async def test_mtime_change_invalidates(cache, tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"v1")

    counter = {"n": 0}

    async def parse(path: Path) -> str:
        counter["n"] += 1
        return f"version {counter['n']}: {path.read_bytes().decode()}"

    md1 = await cache.get_or_parse(src, parse)
    # Force mtime to advance.
    time.sleep(0.01)
    src.write_bytes(b"v2")
    md2 = await cache.get_or_parse(src, parse)
    assert md1 != md2
    assert counter["n"] == 2


@pytest.mark.asyncio
async def test_size_change_invalidates(cache, tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"v1")

    counter = {"n": 0}

    async def parse(path: Path) -> str:
        counter["n"] += 1
        return path.read_bytes().decode()

    await cache.get_or_parse(src, parse)
    # Same mtime granularity on fast filesystems can collide; explicitly
    # change the file size to prove that's also part of the key.
    src.write_bytes(b"v1-but-bigger")
    md2 = await cache.get_or_parse(src, parse)
    assert "bigger" in md2
    assert counter["n"] == 2


@pytest.mark.asyncio
async def test_oversized_markdown_not_cached(cache, tmp_path: Path) -> None:
    src = tmp_path / "big.pdf"
    src.write_bytes(b"raw")
    huge = "x" * 2048  # max_entry_bytes is 1024 in the fixture

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return huge

    md1 = await cache.get_or_parse(src, parse)
    md2 = await cache.get_or_parse(src, parse)
    assert md1 == huge
    assert md2 == huge
    # Both calls re-parsed because the result was too large to cache.
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_lru_evicts_oldest(cache, tmp_path: Path) -> None:
    """Cache size is 3.  Inserting 4 distinct files must evict the first."""
    files = []
    for i in range(4):
        f = tmp_path / f"f{i}.pdf"
        f.write_bytes(f"content {i}".encode())
        files.append(f)
        # Stagger atimes so the LRU ordering is unambiguous on fast disks.
        time.sleep(0.01)

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return path.name

    for f in files:
        await cache.get_or_parse(f, parse)
        time.sleep(0.01)
    assert calls["n"] == 4
    # f0 was evicted (oldest atime); re-reading must re-parse.
    await cache.get_or_parse(files[0], parse)
    assert calls["n"] == 5
    # f3 was the most recent — still cached.
    await cache.get_or_parse(files[3], parse)
    assert calls["n"] == 5


@pytest.mark.asyncio
async def test_unreadable_stat_falls_back_to_parse(cache, tmp_path: Path) -> None:
    """If the source path can't be stat-ed, the cache must still call the
    parser instead of erroring or hanging.  Exercised by a non-existent path."""
    missing = tmp_path / "does_not_exist.pdf"

    calls = {"n": 0}

    async def parse(path: Path) -> str:
        calls["n"] += 1
        return "parsed-anyway"

    md = await cache.get_or_parse(missing, parse)
    assert md == "parsed-anyway"
    assert calls["n"] == 1
