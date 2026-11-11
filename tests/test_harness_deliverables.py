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
    """Many dots must not drive the filename scan quadratic: four times the dots cost
    about four times as much, where a quadratic scan costs sixteen.

    Each cost is the scan's own CPU time, the least of three runs: a busy host slows
    every run alike, and takes the thread off its CPU without counting that, so the
    ratio is the scan's and not the host's load.
    """
    import time

    def cost(dots: int) -> float:
        user = "." * dots + " done"
        runs = []
        for _ in range(3):
            start = time.thread_time()
            missing_deliverables(user, [])
            runs.append(time.thread_time() - start)
        return min(runs)

    small, large = cost(2_500), cost(10_000)
    assert large < 8 * small, (small, large)


def test_nudge_names_the_missing_files():
    text = deliverable_nudge(["report.docx", "data.xlsx"])
    assert "report.docx" in text and "data.xlsx" in text
    assert text.startswith("[System:")



# ---------------------------------------------------------------------------
# Inputs are not deliverables
# ---------------------------------------------------------------------------


def test_an_input_already_in_the_workspace_is_not_asked_for():
    """The model answered from data.csv without naming it in a tool call (a search, a script)."""
    user = "What was the total in data.csv last quarter?"
    messages = [_tool_call("terminal", '{"command": "python sum.py"}')]
    assert missing_deliverables(user, messages, present={"data.csv"}) == []


def test_an_output_asked_for_and_never_written_is_asked_for():
    user = "Summarise the quarter and save it as summary.md."
    assert missing_deliverables(user, [], present={"data.csv"}) == ["summary.md"]


def test_an_output_asked_for_and_written_is_not_asked_for():
    user = "Summarise the quarter and save it as summary.md."
    messages = [_tool_call("write_file", '{"path": "summary.md"}')]
    assert missing_deliverables(user, messages, present={"data.csv"}) == []


def test_a_mixed_request_asks_only_for_the_output():
    user = (
        "Using sales.xlsx and the attached targets.csv, compare them with notes.md "
        "and write the result to report.docx."
    )
    present = {"sales.xlsx", "targets.csv", "notes.md"}
    assert missing_deliverables(user, [], present=present) == ["report.docx"]


def test_inputs_named_by_phrasing_are_not_asked_for_even_outside_the_workspace():
    user = "Using sales.xlsx, based on plan.md and from the uploaded log.txt, produce `review.pdf`."
    assert missing_deliverables(user, []) == ["review.pdf"]


def test_the_officeqa_prompt_asks_for_nothing():
    """A pattern for documents behind tools, read through read_document by their real names."""
    user = (
        "You answer a question about real financial documents: U.S. Treasury Bulletins from 1939 "
        "to 2025, one plain-text file per issue named treasury_bulletin_YYYY_MM.txt (tables are in "
        "Markdown).\nThe bulletins are available only through the Treasury-bulletin corpus tools "
        "(list_documents, search_documents, document_outline, grep_document, read_document); there is "
        "no internet access for this task.\n\nQuestion: What were total national defense expenditures "
        "in January 1941?"
    )
    messages = [_tool_call("mcp__officeqa__read_document", '{"name": "treasury_bulletin_1941_01.txt"}')]
    assert missing_deliverables(user, messages) == []


def test_a_name_with_one_upper_case_segment_is_still_a_name():
    user = "Export the table to Q3_REPORT_MM_final.xlsx and README.md, one file per day as log_MM_DD.txt."
    assert missing_deliverables(user, []) == ["Q3_REPORT_MM_final.xlsx", "README.md"]


def test_a_year_placeholder_is_a_pattern():
    assert missing_deliverables("Keep one summary per year, summary_YYYY.md.", []) == []


async def test_the_harness_reads_the_workspace_file_names_once_there_is_something_to_ask_for():
    from types import SimpleNamespace
    from uuid import uuid4

    from surogates.harness.loop import AgentHarness

    listed: list = []

    class Storage:
        async def list_entries(self, bucket, prefix="", limit=None):
            listed.append((bucket, prefix, limit))
            return [{"key": prefix + "inputs/treasury_bulletin_1941_01.txt"}, {"key": prefix + "data.csv"},
                    {"key": prefix + "inputs/"}]

    harness = AgentHarness.__new__(AgentHarness)
    harness._storage = Storage()
    harness._sandbox_pool = None
    session = SimpleNamespace(id=uuid4(), channel="web", config={"storage_bucket": "b"})
    names = await harness._workspace_file_names(session)
    assert names == {"treasury_bulletin_1941_01.txt", "data.csv"}
    assert listed and listed[0][0] == "b" and str(session.id) in listed[0][1]


async def test_a_session_whose_workspace_cannot_be_read_counts_no_inputs():
    from types import SimpleNamespace
    from uuid import uuid4

    from surogates.harness.loop import AgentHarness

    class Broken:
        async def list_entries(self, bucket, prefix="", limit=None):
            raise OSError("storage away")

    harness = AgentHarness.__new__(AgentHarness)
    harness._storage = Broken()
    harness._sandbox_pool = None
    session = SimpleNamespace(id=uuid4(), channel="web", config={"storage_bucket": "b"})
    assert await harness._workspace_file_names(session) == frozenset()
    harness._storage = None
    assert await harness._workspace_file_names(session) == frozenset()
