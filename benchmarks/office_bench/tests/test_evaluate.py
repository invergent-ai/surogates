"""What the trace says the agent did, and when its final word contradicts the checks."""
import json

from obench.checks import CheckResult
from obench.client import Event
from obench.evaluate import claims, find_document, final_message, metrics
from obench.tasks import by_id


def _events():
    return [
        Event(1, "user.message", {"content": "make a deck"}),
        Event(2, "harness.wake", {}),
        Event(3, "llm.request", {"model": "surogate"}),
        Event(4, "tool.call", {"name": "skill_view", "arguments": {"name": "office", "file_path": "references/slide.md"}}),
        Event(5, "llm.response", {"model": "surogate3.7-flash", "message": {"tool_calls": []}}),
        Event(6, "tool.call", {"name": "office_edit", "arguments": json.dumps({"path": "d.pptx", "ops": []})}),
        Event(7, "tool.result", {"name": "office_edit", "content": json.dumps(
            {"document": "d.pptx", "applied": False, "refused": {"op": 0, "reason": "The slide is too full: split it."}})}),
        Event(8, "tool.call", {"name": "office_edit", "arguments": {"path": "d.pptx", "ops": "[...]"}}),
        Event(9, "tool.result", {"name": "office_edit", "content": json.dumps({"error": "\"ops\" is a non-empty list of ops."})}),
        Event(10, "tool.call", {"name": "write_file", "arguments": {"path": "d.pptx", "content": "x"}}),
        Event(11, "tool.result", {"name": "write_file", "content": json.dumps({"error": "d.pptx is a slide deck: use office_edit"})}),
        Event(12, "llm.request", {"model": "surogate"}),
        Event(13, "llm.response", {"model": "surogate3.7-flash", "message": {"content": "Done: 6 slides with a chart."}}),
        Event(14, "office.save_failed", {"path": "d.pptx"}),
    ]


def test_metrics_from_a_trace():
    m = metrics(_events())
    assert m["model_calls"] == 2
    assert m["served_models"] == {"surogate3.7-flash": 2}
    assert m["tool_calls"] == {"skill_view": 1, "office_edit": 2, "write_file": 1}
    assert m["refusals"] == ["op 0: The slide is too full: split it."]
    assert m["office_errors"] == ['office_edit: "ops" is a non-empty list of ops.']
    assert m["other_tool_errors"] == ["write_file: d.pptx is a slide deck: use office_edit"]
    assert m["skill_views"] == ["office/references/slide.md"] and m["read_office_skill"]
    assert m["file_tools_on_documents"] and m["file_tools_on_documents"][0].startswith("write_file")
    assert m["turns"] == 1
    assert m["office_events"][0].startswith("office.save_failed")


def test_final_message_is_the_last_with_text():
    assert final_message(_events()) == "Done: 6 slides with a chart."


def test_claim_contradicted_by_a_failed_check():
    results = [
        CheckResult("a chart on slide 1", False, "no chart", ("chart",)),
        CheckResult("5 slides", True, "5", ("slide",)),
    ]
    c = claims(results, "Done: 6 slides with a chart.", doc_found=True)
    assert c["contradicted"] == ["says 'chart'; a chart on slide 1: no chart"]
    assert c["claims_success_despite_failures"] and not c["admits_a_problem"]


def test_admitting_the_problem_is_not_a_claim_of_success():
    results = [CheckResult("x", False, "no", ())]
    c = claims(results, "I couldn't add the chart: the tool refused it.", doc_found=True)
    assert c["admits_a_problem"] and not c["claims_success_despite_failures"]


def test_no_document_but_reported_done():
    c = claims([CheckResult("document saved", False, "none", ())], "Your report is ready.", doc_found=False)
    assert c["contradicted"][0].startswith("reports the work done; no document")


def test_find_document_falls_back_to_another_name_for_a_create_task():
    task = by_id()["docx-report"]
    meta = {"collected": [{"path": "reports/4dw.docx", "local": "documents/reports/4dw.docx", "input": False}]}
    found, note = find_document(task, meta)
    assert found["path"] == "reports/4dw.docx" and "not four-day-week-report.docx" in note
    edit = by_id()["docx-reword"]
    found, note = find_document(edit, {"collected": [{"path": "other.docx", "local": "x", "input": False}]})
    assert found is None


def test_reading_a_document_not_made_yet_is_not_a_tool_error():
    m = metrics([
        Event(1, "tool.call", {"name": "office_read", "arguments": {"path": "new.pptx"}}),
        Event(2, "tool.result", {"name": "office_read", "content": json.dumps({"error": "No document at new.pptx."})}),
        Event(3, "tool.call", {"name": "list_files", "arguments": {"path": "."}}),
        Event(4, "tool.result", {"name": "list_files", "content": json.dumps({"error": "sandbox_unavailable"})}),
    ])
    assert m["office_errors"] == [] and m["reads_of_missing_document"] == 1
    assert m["file_or_terminal_calls"] == 1 and m["sandbox_unavailable"] == 1


def test_a_full_office_service_is_not_a_model_error():
    m = metrics([Event(1, "tool.result", {"name": "office_read", "content": json.dumps(
        {"error": "The office service has as many documents open as it can hold; try again in a minute."})})])
    assert m["office_errors"] == [] and m["office_busy"] == 1


def test_a_negated_claim_word_is_not_a_claim_and_a_missing_input_is():
    results = [CheckResult("East's total fixed", False, "still =B3+B2", ("fixed",))]
    msg = "I can't complete this: there's no `sales.xlsx` in the workspace, so I can't write a fixed one."
    c = claims(results, msg, doc_found=True, inputs=["sales.xlsx"])
    assert c["contradicted"] == ["says sales.xlsx is not there; it was uploaded before the message"]
    assert c["admits_a_problem"] and not c["claims_success_despite_failures"]
