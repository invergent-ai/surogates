"""A turn that ends without the file the user named by name.

The two positive cases are the real workspace-bench failures that motivated
the check: a report staged into tmp_ drafts and never assembled, and five
sources read without anything written.
"""

from surogates.harness.loop_deliverables import (
    deliverable_nudge,
    missing_deliverables,
)


def _tool_call(name: str, arguments: str) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": name, "arguments": arguments}}
        ],
    }


def test_staged_drafts_never_assembled():
    """fail-001 task 354: six tmp_stage docx, no final document."""
    user = (
        "Draft the plan. The task requires these files by name, spelled "
        "exactly like this: `2025-key-administrative-work-plan.doc`."
    )
    messages = [
        _tool_call("terminal", '{"command": "python build.py tmp_stage1.docx"}'),
        _tool_call("terminal", '{"command": "python build.py tmp_stage6.docx"}'),
    ]
    assert missing_deliverables(user, messages) == [
        "2025-key-administrative-work-plan.doc"
    ]


def test_sources_read_nothing_written():
    """fail-001 task 232: five source txt collected, no report produced."""
    user = "Analyse the annual reports and produce `Investment_Value_2024.docx`."
    messages = [
        _tool_call("read_file", '{"path": "txt/920108_annual_report.txt"}'),
        _tool_call("read_file", '{"path": "txt/920111_annual_report.txt"}'),
    ]
    assert missing_deliverables(user, messages) == ["Investment_Value_2024.docx"]


def test_file_written_is_not_flagged():
    user = "Write the summary to `report.docx`."
    messages = [_tool_call("write_file", '{"path": "outputs/report.docx"}')]
    assert missing_deliverables(user, messages) == []


def test_file_seen_in_a_tool_result_is_not_flagged():
    """A directory listing showing the file is evidence enough."""
    user = "Produce `report.docx`."
    messages = [
        _tool_call("terminal", '{"command": "python make_doc.py"}'),
        {"role": "tool", "content": "outputs/\n  report.docx  44k\n"},
    ]
    assert missing_deliverables(user, messages) == []


def test_input_files_are_not_deliverables():
    """A file the model read is touched, so it never reads as missing."""
    user = "Summarise data.csv for me."
    messages = [_tool_call("read_file", '{"path": "data.csv"}')]
    assert missing_deliverables(user, messages) == []


def test_no_filename_named_means_no_check():
    user = "Write me a short summary of the quarter."
    assert missing_deliverables(user, []) == []


def test_bare_extension_is_not_a_filename():
    user = "Send me the .docx version when you are done."
    assert missing_deliverables(user, []) == []


def test_match_is_case_insensitive():
    user = "Produce `Report.DOCX`."
    messages = [_tool_call("write_file", '{"path": "outputs/report.docx"}')]
    assert missing_deliverables(user, messages) == []


def test_many_names_are_capped():
    names = [f"out{i}.md" for i in range(9)]
    user = "Produce " + ", ".join(f"`{n}`" for n in names)
    assert missing_deliverables(user, [], limit=5) == names[:5]


def test_duplicate_mentions_reported_once():
    user = "Write `report.docx`. Put the totals in `report.docx` too."
    assert missing_deliverables(user, []) == ["report.docx"]


def test_pathological_input_stays_linear():
    """Many dots must not drive the filename scan quadratic."""
    import time

    user = "." * 50_000 + " done"
    start = time.monotonic()
    missing_deliverables(user, [])
    assert time.monotonic() - start < 2.0


def test_nudge_names_the_missing_files():
    text = deliverable_nudge(["report.docx", "data.xlsx"])
    assert "report.docx" in text and "data.xlsx" in text
    assert text.startswith("[System:")
