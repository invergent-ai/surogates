from gaia_bench.report import (
    TaskOutcome,
    find_regressions,
    render,
    summarize,
)


def outcome(tid, level=1, strict=True, lenient=True, flags=None,
            root_cause=None, owner=None, evidence="", hypothesis=""):
    return TaskOutcome(
        task_id=tid, level=level, strict_pass=strict, lenient_pass=lenient,
        flags=flags or [], root_cause=root_cause, owner=owner,
        evidence=evidence, hypothesis=hypothesis,
    )


def test_outcome_defaults_keep_old_files_loadable():
    # outcomes.json written before evidence/hypothesis existed must still load.
    o = TaskOutcome(task_id="a", level=1, strict_pass=True, lenient_pass=True)
    assert o.evidence == ""
    assert o.hypothesis == ""


def test_render_surfaces_evidence_for_attributed_failures():
    # A verdict you cannot check is a verdict you cannot trust.
    text = render([
        outcome("a", strict=False, lenient=False,
                root_cause="page_content_missed", owner="browser",
                evidence="step 12 fetched the page but missed the table",
                hypothesis="widen browser_get_state extraction"),
    ])
    assert "step 12 fetched the page but missed the table" in text
    assert "widen browser_get_state extraction" in text
    assert "page_content_missed" in text


def test_render_omits_the_evidence_section_when_nothing_attributed():
    text = render([outcome("a", strict=True)])
    assert "Attributed failures" not in text


def test_summarize_counts_overall_and_per_level():
    s = summarize([
        outcome("a", level=1, strict=True),
        outcome("b", level=1, strict=False, lenient=False),
        outcome("c", level=2, strict=True),
    ])
    assert s["total"] == 3
    assert s["strict_passed"] == 2
    assert s["by_level"][1]["total"] == 2
    assert s["by_level"][1]["strict_passed"] == 1
    assert s["by_level"][2]["strict_passed"] == 1


def test_summarize_reports_formatting_gap():
    s = summarize([
        outcome("a", strict=False, lenient=True),
        outcome("b", strict=False, lenient=True),
        outcome("c", strict=True, lenient=True),
    ])
    assert s["strict_passed"] == 1
    assert s["lenient_passed"] == 3
    assert s["formatting_gap"] == 2


def test_summarize_ranks_owners_by_recoverable_points():
    s = summarize([
        outcome("a", strict=False, lenient=False,
                root_cause="page_content_missed", owner="browser"),
        outcome("b", strict=False, lenient=False,
                root_cause="page_fetch_failed", owner="browser"),
        outcome("c", strict=False, lenient=False,
                root_cause="file_parse_failed", owner="file_ops"),
        outcome("d", strict=True),
    ])
    ranked = s["fix_list"]
    assert ranked[0]["owner"] == "browser"
    assert ranked[0]["count"] == 2
    assert ranked[0]["recoverable_pct"] == 50.0
    assert ranked[1]["owner"] == "file_ops"


def test_ambiguous_ground_truth_excluded_from_fix_list():
    s = summarize([
        outcome("a", strict=False, lenient=False,
                root_cause="ambiguous_ground_truth", owner="benchmark"),
        outcome("b", strict=False, lenient=False,
                root_cause="page_fetch_failed", owner="browser"),
    ])
    owners = [row["owner"] for row in s["fix_list"]]
    assert "benchmark" not in owners
    assert s["benchmark_ceiling"] == 1


def test_find_regressions_detects_pass_to_fail():
    before = [outcome("a", strict=True), outcome("b", strict=True)]
    after = [outcome("a", strict=True), outcome("b", strict=False)]
    assert find_regressions(before, after) == ["b"]


def test_find_regressions_ignores_new_and_fixed_tasks():
    before = [outcome("a", strict=False), outcome("b", strict=True)]
    after = [outcome("a", strict=True), outcome("b", strict=True),
             outcome("c", strict=False)]
    assert find_regressions(before, after) == []


def test_render_puts_regressions_before_score():
    before = [outcome("a", strict=True)]
    after = [outcome("a", strict=False, lenient=False)]
    text = render(after, previous=before, run_id="run-2")
    assert text.index("Regressions") < text.index("Score")
    assert "a" in text


def test_render_marks_single_run_deltas_provisional():
    text = render([outcome("a")], previous=[outcome("a")], provisional=True)
    assert "provisional" in text.lower()


def test_render_without_previous_omits_regressions_section():
    text = render([outcome("a")], previous=None)
    assert "Regressions" not in text
    assert "Score" in text
