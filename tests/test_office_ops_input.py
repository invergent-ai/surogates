"""office_edit's ops, read as the model meant them: what was read differently is said, anything else is refused."""

import json

import pytest

from surogates.office.agent import AGENT_KINDS
from surogates.office.ops_input import OP_NAMES, OP_SHAPES, OpsInputError, close_missing, read_ops

DELETE = {"op": "delete", "target": {"block": "1", "hash": "h"}}
# As the benchmark saw it: the op's own '}' missing right before the list's ']', five levels deep.
ONE_BRACE_SHORT = (
    '[{"op": "insert", "target": {"end": true}, "content": {"markdown": "# A\\n<?block why?>", '
    '"blocks": {"why": {"type": "pending", "intent": "Why"}}}]'
)


def test_list_passes_unchanged():
    assert read_ops([DELETE], "docx") == ([DELETE], [])


def test_valid_json_string():
    ops, read_as = read_ops('[{"op": "delete", "target": {"block": "1", "hash": "h"}}]', "docx")
    assert ops == [DELETE]
    assert len(read_as) == 1 and "text" in read_as[0]


def test_missing_brace_before_bracket():
    ops, read_as = read_ops(ONE_BRACE_SHORT, "docx")
    assert [op["op"] for op in ops] == ["insert"]
    assert ops[0]["content"]["blocks"]["why"]["intent"] == "Why"
    assert any("missing '}'" in note for note in read_as)


def test_missing_closers_at_end():
    ops, read_as = read_ops('[{"op": "delete", "target": {"block": "1", "hash": "h"', "docx")
    assert ops == [DELETE]
    assert any("'}}]'" in note for note in read_as)


def test_other_breakage_is_refused():
    for broken in (
        '[{"op": "delete" "target": {}}]',  # a missing comma
        '[{"op": "insert", "content": {"markdown": "a"}, {"op": "delete"}]',  # a closer missing mid-list, then another op
    ):
        with pytest.raises(OpsInputError) as refused:
            read_ops(broken, "docx")
        assert "not valid JSON" in str(refused.value) and "character" in str(refused.value)


def test_single_op_object():
    ops, read_as = read_ops(DELETE, "docx")
    assert ops == [DELETE] and any("one op" in note for note in read_as)
    ops, read_as = read_ops(json.dumps(DELETE), "docx")
    assert ops == [DELETE] and any("one op" in note for note in read_as)


def test_op_name_as_key():
    ops, read_as = read_ops([{"text": {"slide": "1", "hash": "h", "shape": "2", "text": "Q3"}}], "pptx")
    assert ops == [{"op": "text", "slide": "1", "hash": "h", "shape": "2", "text": "Q3"}]
    assert read_as
    # The benchmark's other two: a spreadsheet's sheet and a PDF's fill.
    assert read_ops([{"sheet": {"action": "add", "name": "Q3"}}], "xlsx")[0] == [{"op": "sheet", "action": "add", "name": "Q3"}]
    assert read_ops([{"fill": {"fields": {"client": "A"}}}], "pdf")[0] == [{"op": "fill", "fields": {"client": "A"}}]


def test_type_as_op():
    ops, read_as = read_ops([{"type": "fill", "fields": {"client": "A"}}], "pdf")
    assert ops == [{"op": "fill", "fields": {"client": "A"}}] and read_as
    # add_field's own "type" is a field type, not an op: the op it belongs to is named.
    with pytest.raises(OpsInputError) as refused:
        read_ops([{"type": "text", "page": 1, "rect": [1, 1, 2, 2], "name": "n"}], "pdf")
    assert "add_field" in str(refused.value) and OP_SHAPES["pdf"]["add_field"] in str(refused.value)


def test_unknown_op_lists_the_ops_and_the_nearest():
    with pytest.raises(OpsInputError) as refused:
        read_ops([{"op": "rewrite"}], "docx")
    assert "insert, replace, delete, move, comment, reply or resolve" in str(refused.value)
    assert OP_SHAPES["docx"]["replace"] in str(refused.value)


def test_empty_is_refused():
    for empty in ([], "", None):
        with pytest.raises(OpsInputError) as refused:
            read_ops(empty, "docx")
        assert str(refused.value) == '"ops" is a non-empty list of ops.'


def test_an_op_that_is_no_object_is_refused():
    with pytest.raises(OpsInputError) as refused:
        read_ops([DELETE, "delete"], "docx")
    assert "op 1" in str(refused.value) and OP_SHAPES["docx"]["delete"] in str(refused.value)


def test_each_types_ops_and_their_shapes():
    assert OP_NAMES == {
        "docx": ("insert", "replace", "delete", "move", "comment", "reply", "resolve"),
        "xlsx": ("set", "format", "chart", "sheet", "reply", "resolve"),
        "pptx": ("slides", "text", "order", "remove", "reply", "resolve"),
        "pdf": ("pages", "fill", "add_field", "redact", "annotate", "reply", "resolve"),
    }
    assert set(OP_NAMES) == set(AGENT_KINDS)  # every type office_edit opens
    assert OP_SHAPES["pptx"]["text"] == '{"op": "text", "slide": "<id>", "hash": "<hash>", "shape": "<id>", "text": "…"}'
    for shapes in OP_SHAPES.values():
        for name, shape in shapes.items():
            assert json.loads(shape)["op"] == name


def test_close_missing_adds_only_closers():
    assert close_missing('{"a": [1, 2}') == ('{"a": [1, 2]}', ["missing ']' before the '}' at character 11"])
    assert close_missing('[{"a": "}]"') == ('[{"a": "}]"}]', ["missing '}]' at the end"])
    assert close_missing('{"a": "b\\"}"') == ('{"a": "b\\"}"}', ["missing '}' at the end"])
    for other in (
        '{"a": "unterminated',  # a quote
        '[{"a": 1}]]',  # a closer too many
        '[{"a": 1} {"b": 2}',  # a comma
        '[{"a": 1},',  # what ends it is not a closer
        '[1, 2]',  # nothing missing
    ):
        assert close_missing(other) is None
