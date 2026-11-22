"""Binary Word 97-2003 files: read when they are real, and never a crashed grading run when they are not.

``fixtures/legacy_word97.doc`` is a real file: LibreOffice's "MS Word 97" export of a small document with a heading,
non-ASCII text, a date field, a table with empty cells and a page header.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from wsbench.extract import DEFAULT_MAX_CHARS, extract_text
from wsbench.legacy_doc import OLE2_MAGIC, LegacyDocError, NotWordDocument, _has_row_end, extract_legacy_doc

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_word97.doc"
END, FREE, NONE = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF


def _entry(name, kind, start=END, size=0, left=NONE, right=NONE, child=NONE):
    raw = name.encode("utf-16-le") + b"\0\0"
    head = raw.ljust(64, b"\0") + struct.pack("<HBBIII", len(raw), kind, 1, left, right, child)
    return head.ljust(116, b"\0") + struct.pack("<IQ", start, size)


def _compound(streams, *, nested=None):
    """An OLE2 file: *streams* in the root storage, *nested* in a storage below it (large-sector streams only)."""
    body, fat, placed = b"", [], {}
    for name, data in {**streams, **{f"nested/{k}": v for k, v in (nested or {}).items()}}.items():
        padded = data + b"\0" * (-len(data) % 512)
        first, count = len(fat), len(padded) // 512
        fat += [first + i + 1 for i in range(count - 1)] + [END] if count else []
        placed[name] = (first if count else END, len(data))
        body += padded
    names = list(streams)
    entries = [_entry("Root Entry", 5, child=1 if names or nested else NONE)]
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
    dir_first = len(fat)
    dir_count = len(directory) // 512
    fat += [dir_first + i + 1 for i in range(dir_count - 1)] + [END]
    fat_count = 1
    while (len(fat) + fat_count) > fat_count * 128:
        fat_count += 1
    fat_first = len(fat)
    fat += [0xFFFFFFFD] * fat_count
    fat += [FREE] * (fat_count * 128 - len(fat))
    head = bytearray(512)
    head[:8] = OLE2_MAGIC
    struct.pack_into("<HHHHH", head, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIII", head, 44, fat_count, dir_first, 0, 0, END, 0, END, 0)
    for i in range(109):
        struct.pack_into("<I", head, 76 + 4 * i, fat_first + i if i < fat_count else FREE)
    return bytes(head) + body + directory + struct.pack(f"<{len(fat)}I", *fat)


def _word(pieces, *, flags=0x0200, nfib=0xC1, ident=0xA5EC):
    """A Word file whose text is *pieces*: (text, compressed)."""
    fib_size = 32 + 2 + 28 + 2 + 88 + 2 + 8 * 93
    word = bytearray(fib_size)
    struct.pack_into("<HH", word, 0, ident, nfib)
    struct.pack_into("<H", word, 10, flags)
    struct.pack_into("<H", word, 32, 14)
    struct.pack_into("<H", word, 62, 22)
    struct.pack_into("<H", word, 152, 93)
    cps, pcds, cp = [0], b"", 0
    for text, compressed in pieces:
        raw = text.encode("cp1252" if compressed else "utf-16-le")
        fc = len(word) * 2 | 0x40000000 if compressed else len(word)
        pcds += struct.pack("<HIH", 0, fc, 0)
        word += raw
        cp += len(text)
        cps.append(cp)
    struct.pack_into("<I", word, 64 + 12, cp)
    plc = struct.pack(f"<{len(cps)}I", *cps) + pcds
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", word, 154 + 8 * 33, 0, len(clx))
    return _compound({"WordDocument": bytes(word), "1Table": clx})


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
    inner = _word([("embedded text\r", True)])
    path = _write(tmp_path, _compound({"Workbook": b"\xff" * 600}, nested={"WordDocument": inner}))
    text, note = extract_text(path)
    assert text == "" and "not extractable" in note


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
