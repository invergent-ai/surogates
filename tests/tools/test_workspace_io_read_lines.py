"""LocalWorkspaceIO.read_lines: a page of a text file, cut where TextIOWrapper.readlines() ends its lines."""

from __future__ import annotations

import io
import random

import pytest

from surogates.tools.workspace_io import LinePage, LocalWorkspaceIO, local
from surogates.tools.workspace_io.local import CODE_UNITS

# Line ends, NUL, characters of every UTF-8 length, and characters whose code
# units hold the byte 0x0A or 0x0D without being a line end: 上 (U+4E0A),
# 不 (U+4E0D), U+0A0D and U+0D0A.
ALPHABET = ["a", "bb", "é", "€", "😀", "\n", "\r\n", "\r", "x" * 7, "中文", "\x00", "上", "不", "਍", "ഊ"]


def lines_of(data: bytes, encoding: str) -> list[str]:
    return io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors="replace").readlines()


def decoded(page: LinePage, encoding: str) -> list[str]:
    # A page holds no utf-8-sig BOM.
    return lines_of(page.data, "utf-8" if encoding == "utf-8-sig" else encoding)


def text(rng: random.Random, encoding: str) -> bytes:
    """Random text in *encoding*, at times with invalid sequences, lone surrogates and a last unit cut off."""
    data = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 40))).encode(encoding)
    if rng.random() < 0.3:
        data = bytes(rng.choice([byte, 0x80, 0xFF, 0xD8, 0x0A, 0x0D, 0x00]) for byte in data)
    if rng.random() < 0.1:
        data = data[:-1]
    if encoding == "utf-8-sig" and not data.startswith(b"\xef\xbb\xbf"):
        # read_file pages utf-8-sig only for a file that starts with its whole BOM.
        data = b"\xef\xbb\xbf" + data
    return data


@pytest.fixture
def page(tmp_path):
    """read_lines of *data*, written to a file of its own."""
    path = tmp_path / "f.txt"
    wio = LocalWorkspaceIO(str(tmp_path))

    async def read(data: bytes, encoding: str = "utf-8", offset: int = 1, limit: int = 2000, max_bytes: int = 1 << 30):
        path.write_bytes(data)
        return await wio.read_lines(str(path), encoding=encoding, offset=offset, limit=limit, max_bytes=max_bytes)

    return read


@pytest.mark.parametrize("piece_bytes", [4, 12, 1024 * 1024])
async def test_a_page_holds_the_lines_readlines_finds(page, monkeypatch, piece_bytes):
    # Pieces of a few bytes put line ends, and CR LFs, across every boundary.
    monkeypatch.setattr(local, "_PIECE_BYTES", piece_bytes)
    rng = random.Random(piece_bytes)
    for _ in range(3000):
        encoding = rng.choice(list(CODE_UNITS))
        data = text(rng, encoding)
        offset, limit = rng.randint(1, 12), rng.randint(-14, 8)
        got = await page(data, encoding, offset, limit)
        lines = lines_of(data, encoding)
        assert got.total_lines == len(lines), (data, encoding)
        # As Python slices them, a limit below one included.
        assert decoded(got, encoding) == lines[offset - 1:min(offset - 1 + limit, len(lines))], (
            data, encoding, offset, limit,
        )


async def test_a_page_holds_the_whole_lines_that_fit_or_the_first_ones_head(page, monkeypatch):
    monkeypatch.setattr(local, "_PIECE_BYTES", 8)
    rng = random.Random(7)
    for _ in range(2000):
        encoding = rng.choice(list(CODE_UNITS))
        data = text(rng, encoding)
        offset, limit, max_bytes = rng.randint(1, 8), rng.randint(1, 6), rng.randint(0, 40)
        # The bytes of the window's first n lines, n = 0 to limit.
        heads = [(await page(data, encoding, offset, n)).data for n in range(limit + 1)]
        got = await page(data, encoding, offset, limit, max_bytes)
        if len(heads[1]) > max_bytes:
            assert got.data == heads[1][:max_bytes], (data, encoding, offset, limit, max_bytes)
        else:
            assert got.data == max(head for head in heads if len(head) <= max_bytes), (
                data, encoding, offset, limit, max_bytes,
            )


async def test_an_empty_file_and_a_utf8_bom_alone_have_no_lines(page):
    assert await page(b"") == LinePage(b"", 0)
    assert await page(b"\xef\xbb\xbf", "utf-8-sig") == LinePage(b"", 0)
    # Any other codec keeps its BOM as a character of line 1.
    assert await page(b"\xff\xfe", "utf-16-le") == LinePage(b"\xff\xfe", 1)


async def test_a_page_past_the_end_or_of_no_lines_is_empty(page):
    data = b"one\ntwo\nthree\n"
    assert await page(data, offset=4) == LinePage(b"", 3)
    assert await page(data, limit=0) == LinePage(b"", 3)
    assert await page(data, offset=2, limit=-1) == LinePage(b"", 3)
    # lines[0:-1]: every line but the last.
    assert await page(data, limit=-1) == LinePage(b"one\ntwo\n", 3)


async def test_a_first_line_longer_than_the_page_is_its_head(page):
    assert await page(b"x" * 100 + b"\nshort\n", max_bytes=10) == LinePage(b"x" * 10, 2)
    assert await page(b"ab\ncd\n", limit=2, max_bytes=4) == LinePage(b"ab\n", 2)


async def test_a_cr_lf_across_two_pieces_ends_one_line(page):
    data = b"x" * (1024 * 1024 - 1) + b"\r\ny\r\n"
    assert await page(data, offset=2) == LinePage(b"y\r\n", 2)
    utf16 = ("x" * (512 * 1024 - 1) + "\r\ny\r\n").encode("utf-16-le")
    assert await page(utf16, "utf-16-le", offset=2) == LinePage("y\r\n".encode("utf-16-le"), 2)


async def test_a_utf16_page_starts_at_its_line(page):
    data = "﻿one\ntwo\nthree\n".encode("utf-16-le")
    assert await page(data, "utf-16-le", offset=2) == LinePage("two\nthree\n".encode("utf-16-le"), 3)


async def test_a_file_that_cannot_be_read_fails_as_a_read_does(tmp_path):
    wio = LocalWorkspaceIO(str(tmp_path))
    for key in (str(tmp_path / "missing"), str(tmp_path)):
        with pytest.raises(OSError) as paged:
            await wio.read_lines(key, encoding="utf-8", offset=1, limit=1, max_bytes=1)
        with pytest.raises(OSError) as read:
            await wio.read(key)
        assert (type(paged.value), str(paged.value)) == (type(read.value), str(read.value))
