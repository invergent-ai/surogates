"""Text of a binary Word 97-2003 ``.doc``, with the standard library only.

The judge's machines have no LibreOffice, so this reads the two layers itself:
the OLE2 compound file ([MS-CFB]) for the streams, and the Word binary format
([MS-DOC]) for the main document's text through its piece table.  Like the
``.docx`` extractor it returns the body and its tables, without headers,
footers, notes or text boxes.  Field codes are left out and their results kept.

Anything malformed raises :class:`LegacyDocError`, which the extractor reports
as a failed extraction.  An OLE2 file that holds no Word document (a ``.xls``
under a ``.doc`` name) raises :class:`NotWordDocument`.
"""
from __future__ import annotations

import bisect
import struct

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_MAX_FILE_BYTES = 200 * 1024 * 1024
_END_OF_CHAIN = 0xFFFFFFFE
_FREE_SECTOR = 0xFFFFFFFF
_NO_STREAM = 0xFFFFFFFF
_DIR_ENTRY_BYTES = 128

_WORD_IDENT = 0xA5EC
_FIRST_WORD97_NFIB = 0xC0
_F_ENCRYPTED = 0x0100
_F_WHICH_TABLE = 0x0200
_FC_LCB_PLCF_BTE_PAPX = 13
_FC_LCB_CLX = 33
_FKP_BYTES = 512
_SPRM_P_F_TTP = 0x2417
_SPRM_P_F_INNER_TTP = 0x244C
_SPRM_T_DEF_TABLE = 0xD608
_SPRM_P_CHG_TABS = 0xC615
_SPRM_P_HUGE_PAPX = 0x6646

_CELL_SEPARATOR = " | "


class LegacyDocError(ValueError):
    """The file is not a readable Word 97-2003 document."""


class NotWordDocument(LegacyDocError):
    """An OLE2 file without a Word document in it."""


def is_ole2(path: str) -> bool:
    with open(path, "rb") as fh:
        return fh.read(len(OLE2_MAGIC)) == OLE2_MAGIC


def _u16(data: bytes, offset: int) -> int:
    try:
        return struct.unpack_from("<H", data, offset)[0]
    except struct.error as exc:
        raise LegacyDocError(f"truncated at byte {offset}") from exc


def _u32(data: bytes, offset: int) -> int:
    try:
        return struct.unpack_from("<I", data, offset)[0]
    except struct.error as exc:
        raise LegacyDocError(f"truncated at byte {offset}") from exc


class _CompoundFile:
    """The root storage's streams of an OLE2 file."""

    def __init__(self, data: bytes) -> None:
        if data[:8] != OLE2_MAGIC or len(data) < 512:
            raise LegacyDocError("no OLE2 header")
        if _u16(data, 28) != 0xFFFE:
            raise LegacyDocError("bad byte order mark")
        self.data = data
        self.sector_bytes = 1 << _u16(data, 30)
        self.mini_bytes = 1 << _u16(data, 32)
        if self.sector_bytes not in (512, 4096) or self.mini_bytes != 64:
            raise LegacyDocError("unsupported sector size")
        self.mini_cutoff = _u32(data, 56)
        self.fat = self._read_fat()
        self.mini_fat = self._table(_u32(data, 60))
        directory = self._chain_bytes(_u32(data, 48), self.fat)
        self.entries = [
            directory[i:i + _DIR_ENTRY_BYTES]
            for i in range(0, len(directory) - _DIR_ENTRY_BYTES + 1, _DIR_ENTRY_BYTES)
        ]
        if not self.entries or self.entries[0][66] != 5:
            raise LegacyDocError("no root storage")
        root = self.entries[0]
        self.mini_stream = self._chain_bytes(_u32(root, 116), self.fat)[:_u32(root, 120)]
        self.streams = self._root_children()

    def _sector(self, number: int) -> bytes:
        start = (number + 1) * self.sector_bytes
        end = start + self.sector_bytes
        if number >= 0xFFFFFFFA or end > len(self.data):
            raise LegacyDocError(f"sector {number} is outside the file")
        return self.data[start:end]

    def _read_fat(self) -> list[int]:
        sectors = [_u32(self.data, 76 + 4 * i) for i in range(109)]
        next_difat, seen = _u32(self.data, 68), set()
        per_sector = self.sector_bytes // 4 - 1
        while next_difat not in (_END_OF_CHAIN, _FREE_SECTOR):
            if next_difat in seen:
                raise LegacyDocError("the sector allocation table loops")
            seen.add(next_difat)
            page = self._sector(next_difat)
            sectors += [_u32(page, 4 * i) for i in range(per_sector)]
            next_difat = _u32(page, 4 * per_sector)
        fat: list[int] = []
        for sector in sectors:
            if sector == _FREE_SECTOR:
                continue
            page = self._sector(sector)
            fat += struct.unpack(f"<{len(page) // 4}I", page)
        return fat

    def _table(self, start: int) -> list[int]:
        if start in (_END_OF_CHAIN, _FREE_SECTOR):
            return []
        raw = self._chain_bytes(start, self.fat)
        return list(struct.unpack(f"<{len(raw) // 4}I", raw[: len(raw) - len(raw) % 4]))

    def _chain(self, start: int, table: list[int]) -> list[int]:
        chain: list[int] = []
        seen: set[int] = set()
        sector = start
        while sector != _END_OF_CHAIN:
            if sector in seen or sector >= len(table):
                raise LegacyDocError("a sector chain loops or leaves the file")
            seen.add(sector)
            chain.append(sector)
            sector = table[sector]
        return chain

    def _chain_bytes(self, start: int, table: list[int]) -> bytes:
        if start == _END_OF_CHAIN:
            return b""
        return b"".join(self._sector(s) for s in self._chain(start, table))

    def _root_children(self) -> dict[str, bytes]:
        """Only the root's own streams: an embedded object holds a ``WordDocument`` of its own."""
        found: dict[str, bytes] = {}
        pending, seen = [_u32(self.entries[0], 76)], set()
        while pending:
            index = pending.pop()
            if index == _NO_STREAM:
                continue
            if index in seen or index >= len(self.entries):
                raise LegacyDocError("the directory tree loops or leaves the file")
            seen.add(index)
            entry = self.entries[index]
            pending += [_u32(entry, 68), _u32(entry, 72)]
            if entry[66] == 2:
                length = _u16(entry, 64)
                if not 2 <= length <= 64:
                    raise LegacyDocError("bad directory name")
                found[entry[: length - 2].decode("utf-16-le", "replace")] = entry
        return found

    def stream(self, name: str) -> bytes | None:
        entry = self.streams.get(name)
        if entry is None:
            return None
        start, size = _u32(entry, 116), _u32(entry, 120)
        if size < self.mini_cutoff:
            parts = []
            for sector in self._chain(start, self.mini_fat) if size else ():
                offset = sector * self.mini_bytes
                if offset + self.mini_bytes > len(self.mini_stream):
                    raise LegacyDocError("a mini sector is outside the mini stream")
                parts.append(self.mini_stream[offset:offset + self.mini_bytes])
            raw = b"".join(parts)
        else:
            raw = self._chain_bytes(start, self.fat)
        if len(raw) < size:
            raise LegacyDocError(f"stream {name} is shorter than its directory entry says")
        return raw[:size]


def _fib_fields(word: bytes) -> tuple[bool, int, list[int]]:
    """(uses table 1, ccpText, the FIB's fc / lcb words)."""
    if _u16(word, 0) != _WORD_IDENT:
        raise NotWordDocument("no Word file information block")
    if _u16(word, 2) < _FIRST_WORD97_NFIB:
        raise LegacyDocError("older than Word 97")
    flags = _u16(word, 10)
    if flags & _F_ENCRYPTED:
        raise LegacyDocError("the document is encrypted")
    offset = 32
    offset += 2 + 2 * _u16(word, offset)
    long_count = _u16(word, offset)
    longs_at = offset + 2
    if long_count < 4:
        raise LegacyDocError("short file information block")
    ccp_text = _u32(word, longs_at + 12)
    offset = longs_at + 4 * long_count
    pair_count = _u16(word, offset)
    if pair_count <= _FC_LCB_CLX:
        raise LegacyDocError("short file information block")
    pairs = [_u32(word, offset + 2 + 4 * i) for i in range(2 * pair_count)]
    return bool(flags & _F_WHICH_TABLE), ccp_text, pairs


def _pieces(table: bytes, fc_clx: int, lcb_clx: int) -> list[tuple[int, int, int, bool]]:
    """Each text piece: (first cp, last cp + 1, file offset, compressed)."""
    if lcb_clx == 0 or fc_clx + lcb_clx > len(table):
        raise LegacyDocError("no piece table")
    pos, end = fc_clx, fc_clx + lcb_clx
    while pos < end and table[pos] == 1:  # property modifiers before the piece table
        pos += 3 + _u16(table, pos + 1)
    if pos >= end or table[pos] != 2:
        raise LegacyDocError("no piece table")
    size = _u32(table, pos + 1)
    plc = pos + 5
    if size < 16 or (size - 4) % 12 or plc + size > end:
        raise LegacyDocError("bad piece table")
    count = (size - 4) // 12
    out = []
    for i in range(count):
        start, stop = _u32(table, plc + 4 * i), _u32(table, plc + 4 * (i + 1))
        if stop < start:
            raise LegacyDocError("bad piece table")
        fc = _u32(table, plc + 4 * (count + 1) + 8 * i + 2)
        compressed = bool(fc & 0x40000000)
        fc &= 0x3FFFFFFF
        out.append((start, stop, fc // 2 if compressed else fc, compressed))
    return out


def _has_row_end(grpprl: bytes, data_stream: bytes = b"") -> bool:
    """Whether a paragraph's property list marks it as a table row's end.  A list too long for its page
    (a wide table's row) is one property pointing into the ``Data`` stream."""
    pos = 0
    while pos + 2 <= len(grpprl):
        sprm = _u16(grpprl, pos)
        pos += 2
        if sprm == _SPRM_P_HUGE_PAPX:
            at = _u32(grpprl, pos)
            size = _u16(data_stream, at)
            if at + 2 + size > len(data_stream):
                raise LegacyDocError("paragraph properties are outside the Data stream")
            return _has_row_end(data_stream[at + 2:at + 2 + size])
        kind = sprm >> 13
        if kind in (0, 1):
            size = 1
        elif kind in (2, 4, 5):
            size = 2
        elif kind == 3:
            size = 4
        elif kind == 7:
            size = 3
        else:  # a length comes first
            if sprm == _SPRM_T_DEF_TABLE:
                if pos + 2 > len(grpprl):
                    return False
                size = _u16(grpprl, pos) + 1
            elif sprm == _SPRM_P_CHG_TABS and pos < len(grpprl) and grpprl[pos] == 255:
                return False  # its length is not stored; what follows cannot be walked
            else:
                if pos >= len(grpprl):
                    return False
                size = grpprl[pos] + 1
        if sprm in (_SPRM_P_F_TTP, _SPRM_P_F_INNER_TTP) and pos < len(grpprl) and grpprl[pos]:
            return True
        pos += size
    return False


def _row_end_ranges(
    word: bytes, table: bytes, fc: int, lcb: int, data_stream: bytes = b"",
) -> list[tuple[int, int]]:
    """The file ranges of paragraphs that end a table row, sorted."""
    if lcb < 12 or (lcb - 4) % 8 or fc + lcb > len(table):
        return []
    count = (lcb - 4) // 8
    ranges: list[tuple[int, int]] = []
    for i in range(count):
        page_at = (_u32(table, fc + 4 * (count + 1) + 4 * i) & 0x3FFFFF) * _FKP_BYTES
        page = word[page_at:page_at + _FKP_BYTES]
        if len(page) < _FKP_BYTES:
            raise LegacyDocError("a paragraph page is outside the file")
        runs = page[_FKP_BYTES - 1]
        for run in range(runs):
            word_offset = page[4 * (runs + 1) + 13 * run]
            if not word_offset:
                continue
            at = 2 * word_offset
            size = page[at]
            if size:
                body = page[at + 1:at + 2 * size]
            else:
                body = page[at + 2:at + 2 + 2 * page[at + 1]]
            if _has_row_end(body[2:], data_stream):
                ranges.append((_u32(page, 4 * run), _u32(page, 4 * (run + 1))))
    ranges.sort()
    return ranges


class _TextBuilder:
    """Word's control characters as plain text."""

    def __init__(self, row_ends: list[tuple[int, int]]) -> None:
        self.parts: list[str] = []
        self.fields: list[bool] = []  # one per open field: True while in its code
        self.row_ends = row_ends
        self.row_starts = [start for start, _ in row_ends]
        self.after_cell = False

    def _ends_row(self, file_offset: int) -> bool:
        if self.row_ends:
            i = bisect.bisect_right(self.row_starts, file_offset) - 1
            return i >= 0 and file_offset < self.row_ends[i][1]
        return self.after_cell  # no paragraph data: two marks in a row end the row

    def add(self, char: str, file_offset: int) -> None:
        code = ord(char)
        if code == 0x13:
            self.fields.append(True)
        elif code == 0x14:
            if self.fields:
                self.fields[-1] = False
        elif code == 0x15:
            if self.fields:
                self.fields.pop()
        elif any(self.fields):
            return
        elif code == 0x07:
            if self._ends_row(file_offset):
                if self.parts and self.parts[-1] == _CELL_SEPARATOR:
                    self.parts.pop()
                self.parts.append("\n")
                self.after_cell = False
                return
            self.parts.append(_CELL_SEPARATOR)
            self.after_cell = True
            return
        elif code in (0x0B, 0x0C, 0x0D):
            self.parts.append("\n")
        elif code == 0x1E:
            self.parts.append("-")
        elif code == 0x09 or code >= 0x20:
            self.parts.append(char)
        self.after_cell = False

    def text(self) -> str:
        return "".join(self.parts).strip("\n")


def extract_legacy_doc(path: str) -> str:
    """The main document's text of the Word 97-2003 file at *path*."""
    with open(path, "rb") as fh:
        data = fh.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise LegacyDocError("file too large")
    compound = _CompoundFile(data)
    word = compound.stream("WordDocument")
    if word is None:
        raise NotWordDocument("no WordDocument stream")
    uses_table_1, ccp_text, pairs = _fib_fields(word)
    table = compound.stream("1Table" if uses_table_1 else "0Table")
    if table is None:
        raise LegacyDocError("no table stream")
    pieces = _pieces(table, pairs[2 * _FC_LCB_CLX], pairs[2 * _FC_LCB_CLX + 1])
    try:
        row_ends = _row_end_ranges(
            word, table, pairs[2 * _FC_LCB_PLCF_BTE_PAPX], pairs[2 * _FC_LCB_PLCF_BTE_PAPX + 1],
            compound.stream("Data") or b"",
        )
    except (LegacyDocError, IndexError):
        row_ends = []  # the text is still right; only where table rows end is then a guess
    builder = _TextBuilder(row_ends)
    for start, stop, offset, compressed in sorted(pieces):
        if start >= ccp_text:
            break
        count = min(stop, ccp_text) - start
        width = 1 if compressed else 2
        raw = word[offset:offset + count * width]
        if len(raw) < count * width:
            raise LegacyDocError("a text piece is outside the WordDocument stream")
        chars = raw.decode("cp1252", "replace") if compressed else raw.decode("utf-16-le", "replace")
        if len(chars) != count:  # a surrogate pair decodes to one character: keep offsets by unit
            chars = "".join(
                raw[i:i + 2].decode("utf-16-le", "replace") for i in range(0, len(raw), 2)
            )
        for index, char in enumerate(chars):
            builder.add(char, offset + index * width)
    return builder.text()
