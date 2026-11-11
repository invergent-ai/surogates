"""A turn that ends without the file the user named by name.

The two positive cases are the real workspace-bench failures that motivated
the check: a report staged into tmp_ drafts and never assembled, and five
sources read without anything written.
"""

import pytest

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


@pytest.mark.parametrize("user, want", [
    # The clause ends at a colon, a comma, a sentence's end or a line break.
    ("Create the report from the source: report.md", ["report.md"]),
    ("Create a dashboard from the data, dashboard.html", ["dashboard.html"]),
    ("Based on the provided data: report.docx", ["report.docx"]),
    ("Summarize what you read.\n\nreport.docx", ["report.docx"]),
    ("Make a checklist I can use: checklist.md", ["checklist.md"]),
    ("Produce from the attached files: report.docx, summary.xlsx", ["report.docx", "summary.xlsx"]),
    # Nouns are not the verbs that mark an input.
    ("Write the review document review_notes.docx", ["review_notes.docx"]),
    ("Generate the test input file test_input.json", ["test_input.json"]),
    ("Use report_final.docx as the output file name", ["report_final.docx"]),
    ("Turn the notes from the meeting into minutes.docx", ["minutes.docx"]),
])
def test_a_deliverable_near_an_input_word_is_still_asked_for(user, want):
    assert missing_deliverables(user, []) == want


@pytest.mark.parametrize("user", [
    "Post this week's numbers to status.md",
    "Update the existing tracker.xlsx",
    "Fill in the attached budget.xlsx",
    "Save the totals as summary.md",
])
def test_a_file_in_the_workspace_the_request_asks_to_write_is_still_asked_for(user):
    name = user.rsplit(" ", 1)[-1]
    assert missing_deliverables(user, [], present={name.lower()}) == [name]


@pytest.mark.parametrize("user, want", [
    ("Save the result as **report.docx**.", ["report.docx"]),
    ("Deliverable: *summary.md*", ["summary.md"]),
    ("Write the output to [results.csv].", ["results.csv"]),
    ("Put it in <b>report.html</b>", ["report.html"]),
    ("Fill in Form_N.pdf and Appendix_X.docx.", ["Form_N.pdf", "Appendix_X.docx"]),
])
def test_marked_up_names_and_single_letter_segments_are_names(user, want):
    assert missing_deliverables(user, []) == want


@pytest.mark.parametrize("user, present, want", [
    # The words before a path, not before its last segment, tell.
    ("Save it to outputs/report.docx", {"report.docx"}, ["report.docx"]),
    ("Post this week's numbers to reports/status.md", {"status.md"}, ["status.md"]),
    ("Read from inputs/data.csv and answer.", (), []),
    # A list of required names asks for each, even one already there.
    ("The task requires these files by name, spelled exactly like this: `report.docx`.", {"report.docx"},
     ["report.docx"]),
    ("Revise report.docx with the new numbers.", {"report.docx"}, ["report.docx"]),
    # ... and only in its own sentence.
    ("The task requires these files by name: `out.docx`.\nYour inputs: data.csv and notes.md.",
     {"data.csv", "notes.md"}, ["out.docx"]),
    ("Write report.docx. The task requires these files by name: `report.docx`. Use the figures in data.csv.",
     {"data.csv"}, ["report.docx"]),
    ("Compile notes.md and ideas.md into summary.md", {"notes.md", "ideas.md"}, ["summary.md"]),
    # "to" and "as" in these pairs refer to an input.
    ("Refer to the attached policy.pdf when answering.", {"policy.pdf"}, []),
    ("Compared to budget.xlsx, how did we do?", {"budget.xlsx"}, []),
    ("Use a layout such as template.docx.", {"template.docx"}, []),
])
def test_paths_required_lists_and_references(user, present, want):
    assert missing_deliverables(user, [], present=present) == want


def test_a_name_repeated_many_times_is_weighed_a_few_times():
    import time

    user = "x.txt " * 200_000
    start = time.thread_time()
    assert missing_deliverables(user, []) == ["x.txt"]
    assert time.thread_time() - start < 5


def test_every_mention_counts_not_only_the_first():
    user = (
        "Use summary.md as the output file. The task requires these files by name, "
        "spelled exactly like this: `summary.md`."
    )
    assert missing_deliverables(user, []) == ["summary.md"]
    user = "Read notes.md, then write notes.md again with the totals."
    assert missing_deliverables(user, [], present={"notes.md"}) == ["notes.md"]


def test_a_name_that_is_only_ever_an_input_is_not_asked_for():
    user = "Using notes.md, list the totals. Everything you need is from notes.md."
    assert missing_deliverables(user, []) == []


@pytest.mark.parametrize("user", [
    "One file per issue, treasury_bulletin_yyyy_mm.txt.",
    "One file per day, app_YYYYMMDD.json.",
    "Each run writes report_DATE.pdf.",
    "Files are {TICKER}_10K.txt, <id>-notes.md and Q{n}_summary.md.",
    "Look it up in a filing, e.g. 10K_2023.pdf.",
])
def test_patterns_templates_and_examples_are_not_names(user):
    assert missing_deliverables(user, []) == []


def test_a_pasted_listing_against_a_large_tool_output_stays_linear():
    """A pasted log of file names and a megabyte of tool output: four times the
    names cost about four times as much (thread CPU time, least of three runs)."""
    import time

    output = [{"role": "tool", "content": "x" * 1_000_000 + " part-000007.json"}]

    def cost(lines: int) -> float:
        user = "\n".join(f"loading part-{i:06d}.json" for i in range(lines))
        runs = []
        for _ in range(3):
            start = time.thread_time()
            missing_deliverables(user, output)
            runs.append(time.thread_time() - start)
        return min(runs)

    small, large = cost(5_000), cost(20_000)
    assert large < 8 * small, (small, large)


# ---------------------------------------------------------------------------
# The harness's side: the workspace's files
# ---------------------------------------------------------------------------


class _Storage:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.listed: list[tuple] = []

    async def list_entries(self, bucket, prefix="", limit=None):
        self.listed.append((bucket, prefix, limit))
        return [{"key": k} for k in sorted(self.keys) if k.startswith(prefix)][:limit]


def _harness(storage, pool=None):
    from surogates.harness.loop import AgentHarness

    harness = AgentHarness.__new__(AgentHarness)
    harness._storage = storage
    harness._sandbox_pool = pool
    return harness


def _session(**config):
    from types import SimpleNamespace
    from uuid import uuid4

    return SimpleNamespace(id=uuid4(), channel="web", config={"storage_bucket": "b", **config})


def _prefix(session) -> str:
    from surogates.session.attachment_ingest import workspace_root_id
    from surogates.storage.tenant import boundary_workspace_key

    return boundary_workspace_key(session.config, session, workspace_root_id(session), "")


async def test_the_workspace_names_are_the_users_files_and_uploads():
    session = _session()
    p = _prefix(session)
    storage = _Storage([
        p + "data.csv", p + "inputs/treasury_bulletin_1941_01.txt", p + "inputs/",
        p + "vendor/lib/deep/README.md", p + ".agents/notes.md", p + "__pycache__/x.json",
        p + "uploads/telegram/1712345678.5-0-budget.xlsx", p + "uploads/slack/fetch/F07ABC-plan.md",
        p + "uploads/brief.pdf",
    ])
    names = await _harness(storage)._workspace_file_names(session)
    assert names == {
        "data.csv", "treasury_bulletin_1941_01.txt",
        "1712345678.5-0-budget.xlsx", "budget.xlsx", "f07abc-plan.md", "plan.md", "brief.pdf",
    }
    # The uploads are listed on their own, past any cap on the whole workspace.
    assert [prefix for _, prefix, _ in storage.listed] == [p, p + "uploads/"]


async def test_a_helper_session_reads_its_roots_workspace():
    from uuid import uuid4

    root = uuid4()
    session = _session(sandbox_root_session_id=str(root))
    storage = _Storage([])
    await _harness(storage)._workspace_file_names(session)
    assert str(root) in storage.listed[0][1] and str(session.id) not in storage.listed[0][1]


async def test_a_session_whose_workspace_cannot_be_read_counts_no_inputs(monkeypatch):
    import asyncio

    import surogates.harness.loop as loop

    class Broken:
        async def list_entries(self, bucket, prefix="", limit=None):
            raise OSError("storage away")

    class Stalled:
        async def list_entries(self, bucket, prefix="", limit=None):
            await asyncio.sleep(60)

    session = _session()
    assert await _harness(Broken())._workspace_file_names(session) == frozenset()
    assert await _harness(None)._workspace_file_names(session) == frozenset()
    monkeypatch.setattr(loop, "_WORKSPACE_NAMES_TIMEOUT", 0.05)
    assert await _harness(Stalled())._workspace_file_names(session) == frozenset()


async def test_a_thread_on_a_copy_counts_no_inputs(monkeypatch):
    import surogates.harness.loop as loop

    monkeypatch.setattr(loop, "writes_to_copy", lambda pool, owner, config: True)
    storage = _Storage(["anything"])
    assert await _harness(storage)._workspace_file_names(_session()) == frozenset()
    assert storage.listed == []


async def test_the_nudge_leaves_out_a_workspace_input_and_keeps_the_output():
    session = _session()
    p = _prefix(session)
    storage = _Storage([p + "sales.xlsx", p + "tracker.xlsx"])
    harness = _harness(storage)
    user = "Total the year in sales.xlsx, update tracker.xlsx and write it up in report.docx."
    assert await harness._absent_deliverables(session, user, []) == ["tracker.xlsx", "report.docx"]


async def test_the_workspace_is_read_only_when_there_is_something_to_nudge_about():
    session = _session()
    storage = _Storage([])
    harness = _harness(storage)
    messages = [_tool_call("write_file", '{"path": "report.docx"}')]
    assert await harness._absent_deliverables(session, "Write report.docx.", messages) == []
    assert storage.listed == []
