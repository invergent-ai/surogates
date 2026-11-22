"""Binary Word 97-2003 files: read when they are real, and never a crashed grading run when they are not.

``fixtures/legacy_word97.doc`` is a real file: LibreOffice's "MS Word 97" export of a small document with a heading,
non-ASCII text, a date field, a table with empty cells and a page header.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

from wsbench.extract import DEFAULT_MAX_CHARS, extract_text
from wsbench.legacy_doc import (
    OLE2_MAGIC,
    LegacyDocError,
    NotWordDocument,
    _has_row_end,
    _PropertyBudget,
    _row_end_ranges,
    extract_legacy_doc,
)

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_word97.doc"
END, FREE, NONE = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF


def _entry(name, kind, start=END, size=0, left=NONE, right=NONE, child=NONE):
    raw = name.encode("utf-16-le") + b"\0\0"
    head = raw.ljust(64, b"\0") + struct.pack("<HBBIII", len(raw), kind, 1, left, right, child)
    return head.ljust(116, b"\0") + struct.pack("<IQ", start, size)


def _compound(streams, *, nested=None, mini=False, difat=False):
    """An OLE2 file: *streams* in the root storage, *nested* in a storage below it.  *mini*: the streams live in
    the mini stream.  *difat*: the FAT sectors are named by a DIFAT sector instead of the header."""
    body, fat, placed = b"", [], {}

    def place(data):
        nonlocal body
        padded = data + b"\0" * (-len(data) % 512)
        first, count = len(fat), len(padded) // 512
        fat.extend([first + i + 1 for i in range(count - 1)] + [END] if count else [])
        body += padded
        return first if count else END

    all_streams = {**streams, **{f"nested/{k}": v for k, v in (nested or {}).items()}}
    root_start, root_size, mini_fat_first, mini_fat_sectors = END, 0, END, 0
    if mini:
        container, table = b"", []
        for name, data in all_streams.items():
            padded = data + b"\0" * (-len(data) % 64)
            first, count = len(table), len(padded) // 64
            table += [first + i + 1 for i in range(count - 1)] + [END]
            placed[name] = (first, len(data))
            container += padded
        raw_table = struct.pack(f"<{len(table)}I", *table)
        raw_table += b"\xff" * (-len(raw_table) % 512)
        root_start, root_size = place(container), len(container)
        mini_fat_first, mini_fat_sectors = place(raw_table), len(raw_table) // 512
    else:
        for name, data in all_streams.items():
            placed[name] = (place(data), len(data))
    names = list(streams)
    entries = [_entry("Root Entry", 5, root_start, root_size, child=1 if names or nested else NONE)]
    for i, name in enumerate(names):
        following = i + 2 if i + 1 < len(names) or nested else NONE
        entries.append(_entry(name, 2, *placed[name], right=following))
    if nested:
        at = len(entries)
        entries.append(_entry("ObjectPool", 1, child=at + 1))
        keys = list(nested)
        for i, key in enumerate(keys):
            entries.append(_entry(key, 2, *placed[f"nested/{key}"], right=at + 2 + i if i + 1 < len(keys) else NONE))
    directory = b"".join(entries)
    directory += b"\0" * (-len(directory) % 512)
    dir_first = place(directory)
    difat_sector = None
    if difat:
        difat_sector = len(fat)
        fat.append(0xFFFFFFFC)
        body += b"\0" * 512
    fat_count = 1
    while (len(fat) + fat_count) > fat_count * 128:
        fat_count += 1
    fat_first = len(fat)
    fat += [0xFFFFFFFD] * fat_count
    fat += [FREE] * (fat_count * 128 - len(fat))
    head = bytearray(512)
    head[:8] = OLE2_MAGIC
    struct.pack_into("<HHHHH", head, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into(
        "<IIIIIIII", head, 44, fat_count, dir_first, 0, 4096 if mini else 0, mini_fat_first, mini_fat_sectors,
        difat_sector if difat else END, 1 if difat else 0,
    )
    named = [fat_first + i for i in range(fat_count)]
    for i in range(109):
        struct.pack_into("<I", head, 76 + 4 * i, named[i] if not difat and i < fat_count else FREE)
    if difat:
        page = struct.pack(f"<{fat_count}I", *named) + b"\xff" * (508 - 4 * fat_count) + struct.pack("<I", END)
        at = 512 * difat_sector
        body = body[:at] + page + body[at + 512:]
    return bytes(head) + body + struct.pack(f"<{len(fat)}I", *fat)


def _word_streams(pieces, *, flags=0x0200, nfib=0xC1, ident=0xA5EC, prc=b"", same_fc=False):
    """The WordDocument and table streams of a Word file whose text is *pieces*: (text, compressed)."""
    fib_size = 32 + 2 + 28 + 2 + 88 + 2 + 8 * 93
    word = bytearray(fib_size)
    struct.pack_into("<HH", word, 0, ident, nfib)
    struct.pack_into("<H", word, 10, flags)
    struct.pack_into("<H", word, 32, 14)
    struct.pack_into("<H", word, 62, 22)
    struct.pack_into("<H", word, 152, 93)
    cps, pcds, cp, shared = [0], b"", 0, None
    for text, compressed in pieces:
        raw = text.encode("cp1252" if compressed else "utf-16-le", "surrogatepass")
        at = shared if same_fc and shared is not None else len(word)
        shared = at
        fc = at * 2 | 0x40000000 if compressed else at
        pcds += struct.pack("<HIH", 0, fc, 0)
        if at == len(word):
            word += raw
        cp += len(raw) if compressed else len(raw) // 2
        cps.append(cp)
    struct.pack_into("<I", word, 64 + 12, cp)
    plc = struct.pack(f"<{len(cps)}I", *cps) + pcds
    clx = prc + b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 154 + 8 * 33, 0, len(clx))
    return bytes(word), clx


def _word(pieces, *, flags=0x0200, nfib=0xC1, ident=0xA5EC, prc=b"", same_fc=False, **compound):
    word, clx = _word_streams(pieces, flags=flags, nfib=nfib, ident=ident, prc=prc, same_fc=same_fc)
    return _compound({"WordDocument": word, "1Table" if flags & 0x0200 else "0Table": clx}, **compound)


def _write(tmp_path, data, name="file.doc"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_a_real_doc_reads_as_its_text():
    text, note = extract_text(str(FIXTURE))
    assert note is None
    assert text.splitlines()[:3] == [
        "Fixture heading", "Plain ASCII paragraph.", "Diacritics: Mădălin Țară café. CJK: 财务报告. Euro: €100.",
    ]
    assert text.endswith("Paragraph after the table.")


def test_a_real_docs_table_keeps_its_rows_and_empty_cells():
    lines = extract_text(str(FIXTURE))[0].splitlines()
    assert lines[lines.index("h1 | h2 | h3") + 1:lines.index("h1 | h2 | h3") + 3] == ["a |  | c", " |  | z"]


def test_a_field_keeps_its_result_and_not_its_code():
    text = extract_text(str(FIXTURE))[0]
    assert "DATE" not in text and "yyyy" not in text
    line = next(l for l in text.splitlines() if l.startswith("Before the field"))
    assert line.endswith(" after the field.") and line != "Before the field  after the field."


def test_headers_are_left_out_as_the_docx_extractor_leaves_them_out():
    assert "HEADERTEXT" not in extract_text(str(FIXTURE))[0]


def test_the_30k_cap_is_the_same_for_a_doc(tmp_path):
    path = _write(tmp_path, _word([("x" * (DEFAULT_MAX_CHARS + 5), True)]))
    text, note = extract_text(path)
    assert len(text) == DEFAULT_MAX_CHARS
    assert note == f"truncated to {DEFAULT_MAX_CHARS} of {DEFAULT_MAX_CHARS + 5} chars"


def test_compressed_and_unicode_pieces_join_in_order(tmp_path):
    path = _write(tmp_path, _word([("caf\xe9 €", True), (" Țară 财", False), (" end\r", True)]))
    assert extract_text(path) == ("café € Țară 财 end", None)


def test_field_marks_nest(tmp_path):
    raw = "a\x13 OUTER \x13 INNER \x14hidden\x15 \x14shown\x15b\r"
    assert extract_text(_write(tmp_path, _word([(raw, True)])))[0] == "ashownb"


def test_a_row_end_in_a_property_list_too_long_for_its_page_is_found():
    """Word stores a wide table row's properties in the Data stream, behind one property."""
    row_end = struct.pack("<HB", 0x2416, 1) + struct.pack("<HB", 0x2417, 1)
    cell = struct.pack("<HB", 0x2416, 1)
    data = b"\0" * 7 + struct.pack("<H", len(row_end)) + row_end + struct.pack("<H", len(cell)) + cell
    assert _has_row_end(struct.pack("<HI", 0x6646, 7), data) is True
    assert _has_row_end(struct.pack("<HI", 0x6646, 7 + 2 + len(row_end)), data) is False
    with pytest.raises(LegacyDocError):
        _has_row_end(struct.pack("<HI", 0x6646, len(data) - 1), data + b"\xff")


def test_a_docx_is_read_as_before(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("modern file")
    path = tmp_path / "d.docx"
    doc.save(str(path))
    assert extract_text(str(path)) == ("modern file", None)


def test_ooxml_named_doc_still_reads_as_docx(tmp_path):
    import docx

    doc = docx.Document()
    doc.add_paragraph("modern bytes")
    path = tmp_path / "m.doc"
    doc.save(str(path))
    assert extract_text(str(path)) == ("modern bytes", None)


@pytest.mark.parametrize("content", ["<html><body>web page</body></html>", "{\\rtf1 plain rtf}", "just text"])
def test_a_doc_that_is_really_text_reads_as_before(tmp_path, content):
    assert extract_text(_write(tmp_path, content.encode()))[0] == content


def test_a_spreadsheet_named_doc_is_reported_as_binary(tmp_path):
    path = _write(tmp_path, _compound({"Workbook": b"\x09\x08" + b"\xff" * 600}))
    with pytest.raises(NotWordDocument):
        extract_legacy_doc(path)
    text, note = extract_text(path)
    assert text == "" and "not extractable" in note


def test_a_word_document_inside_an_embedded_object_is_not_the_files_text(tmp_path):
    word, clx = _word_streams([("embedded text\r", True)])
    path = _write(tmp_path, _compound({"Workbook": b"\xff" * 600}, nested={"WordDocument": word, "1Table": clx}))
    text, note = extract_text(path)
    assert text == "" and "not extractable" in note


def test_the_files_own_text_wins_over_an_embedded_document(tmp_path):
    word, clx = _word_streams([("own text\r", True)])
    inner, inner_clx = _word_streams([("embedded text\r", True)])
    path = _write(tmp_path, _compound({"WordDocument": word, "1Table": clx},
                                      nested={"WordDocument": inner, "1Table": inner_clx}))
    assert extract_text(path) == ("own text", None)


def test_characters_beyond_the_16_bit_plane_survive(tmp_path):
    assert extract_text(_write(tmp_path, _word([("a😀b\r", False)]))) == ("a😀b", None)


def test_streams_in_the_mini_stream_are_read(tmp_path):
    assert extract_text(_write(tmp_path, _word([("small file\r", True)], mini=True))) == ("small file", None)


def test_fat_sectors_named_by_a_difat_sector_are_read(tmp_path):
    assert extract_text(_write(tmp_path, _word([("difat file\r", True)], difat=True))) == ("difat file", None)


def test_the_table_stream_the_file_names_is_used(tmp_path):
    assert extract_text(_write(tmp_path, _word([("table zero\r", True)], flags=0))) == ("table zero", None)


def test_property_modifiers_before_the_piece_table_are_skipped(tmp_path):
    prc = b"\x01" + struct.pack("<H", 3) + b"abc"
    assert extract_text(_write(tmp_path, _word([("after prc\r", True)], prc=prc))) == ("after prc", None)


@pytest.mark.parametrize("before", [
    struct.pack("<HI", 0x6412, 0xFFFFFFFF),  # four-byte operand
    struct.pack("<HH", 0x4600, 0xFFFF),  # two-byte operand
    struct.pack("<H", 0xE600) + b"\xff\xff\xff",  # three-byte operand
    struct.pack("<HB", 0xC600, 2) + b"\xff\xff",  # a length byte
    struct.pack("<HH", 0xD608, 4) + b"\xff" * 3,  # a two-byte length that counts itself
    struct.pack("<HBB", 0xC615, 255, 1) + b"\xff" * 4 + b"\x01" + b"\xff" * 3,  # tabs with no stored length
])
def test_row_end_is_found_after_each_property_size(before):
    assert _has_row_end(before + struct.pack("<HB", 0x2417, 1)) is True
    assert _has_row_end(before + struct.pack("<HB", 0x2416, 1)) is False


def test_a_nested_tables_row_end_is_found():
    assert _has_row_end(struct.pack("<HB", 0x244C, 1)) is True


def test_a_closed_field_never_hides_later_text_at_scale(tmp_path):
    count = 200_000
    raw = "\x13\x14" * count + "a" * count + "\x15" * count + "\r"
    started = time.monotonic()
    assert extract_text(_write(tmp_path, _word([(raw, True)])))[0] == "a" * DEFAULT_MAX_CHARS
    assert time.monotonic() - started < 10


def test_a_repeated_fat_sector_is_refused_before_it_is_expanded(tmp_path):
    data = bytearray(_word([("text\r", True)]))
    sector = struct.unpack_from("<I", data, 76)[0]
    for i in range(109):
        struct.pack_into("<I", data, 76 + 4 * i, sector)
    with pytest.raises(LegacyDocError, match="allocation table"):
        extract_legacy_doc(_write(tmp_path, bytes(data)))


def test_pieces_that_repeat_the_same_bytes_are_refused(tmp_path):
    path = _write(tmp_path, _word([("x" * 400, True)] * 50, same_fc=True))
    with pytest.raises(LegacyDocError, match="repeat"):
        extract_legacy_doc(path)
    assert "extraction failed" in extract_text(path)[1]


def test_repeated_paragraph_properties_stop_at_the_budget():
    grpprl = struct.pack("<HB", 0x2416, 1) * 100
    budget = _PropertyBudget(len(grpprl) * 3)
    for _ in range(3):
        _has_row_end(grpprl, b"", budget)
    with pytest.raises(LegacyDocError, match="repeated"):
        _has_row_end(grpprl, b"", budget)


def test_many_references_to_one_paragraph_page_read_it_once():
    page = bytearray(512)
    page[511] = 1
    struct.pack_into("<II", page, 0, 0, 10)
    page[8] = 100
    props = struct.pack("<H", 0) + struct.pack("<HB", 0x2417, 1)
    page[200] = (len(props) + 1) // 2
    page[201:201 + len(props)] = props
    word = bytes(page)
    count = 2_000
    table = struct.pack(f"<{count + 1}I", *range(count + 1)) + struct.pack("<I", 0) * count
    assert _row_end_ranges(word, table, 0, len(table)) == [(0, 10)]


def _corrupt_cases():
    real = FIXTURE.read_bytes()
    looped = bytearray(_word([("text\r", True)]))
    fat_at = (struct.unpack_from("<I", looped, 76)[0] + 1) * 512
    struct.pack_into("<I", looped, fat_at, 0)
    return {
        "header_only": OLE2_MAGIC,
        "truncated_real_file": real[:len(real) // 3],
        "garbage_after_magic": OLE2_MAGIC + b"\xff" * 2000,
        "looping_sector_chain": bytes(looped),
        "encrypted": _word([("secret\r", True)], flags=0x0300),
        "older_than_word_97": _word([("old\r", True)], nfib=0x65),
        "field_never_closed": _word([("before \x13 all the rest\r", True)]),
    }


@pytest.mark.parametrize("case", sorted(_corrupt_cases()))
def test_a_corrupt_doc_degrades_to_a_note(tmp_path, case):
    path = _write(tmp_path, _corrupt_cases()[case])
    with pytest.raises(LegacyDocError):
        extract_legacy_doc(path)
    text, note = extract_text(path)
    assert text == "" and "extraction failed" in note


def test_a_word_stream_with_a_foreign_header_is_binary(tmp_path):
    text, note = extract_text(_write(tmp_path, _word([("x\r", True)], ident=0x1234)))
    assert text == "" and "not extractable" in note
