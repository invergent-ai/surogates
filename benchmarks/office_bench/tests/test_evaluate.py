"""What the trace says the agent did, and when its final word contradicts the checks."""
import json
import pathlib
import re
import shlex
import shutil
import sys
import zipfile

from obench import docs
from obench.checks import CheckResult
from obench.client import Event
from obench.evaluate import claims, evaluate, find_document, final_message, metrics
from obench.render import RENDER_SHELL_ENV
from obench.tasks import FIXTURES as INPUTS, by_id

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
OVERFLOW = "no text overflows its box"
MADE = "made in the editor"
# The editor's name and version as it saves them, assembled so the public-repository scan does not read an address.
EDITOR = "euro-office/" + ".".join(("2", "5", "565", "0"))


def _events():
    return [
        Event(1, "user.message", {"content": "make a deck"}),
        Event(2, "harness.wake", {}),
        Event(3, "llm.request", {"model": "surogate"}),
        Event(4, "tool.call", {"name": "skill_view", "arguments": {"name": "office", "file_path": "references/pptx.md"}}),
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
    assert m["skill_views"] == ["office/references/pptx.md"] and m["read_office_skill"]
    assert m["office_guides"] == []
    assert m["file_tools_on_documents"] and m["file_tools_on_documents"][0].startswith("write_file")
    assert m["turns"] == 1
    assert m["office_events"][0].startswith("office.save_failed")


def test_a_guide_an_office_call_delivered_counts_as_reading_the_office_skill():
    # The harness gives the office skill's rules and the type's reference with the first office call on each type.
    guide = {"rules": "# Working on documents live with the user", "reference": "# Slide decks (.pptx)"}
    refused = "ops is not valid JSON: Expecting value at character 1."
    events = [
        Event(1, "tool.call", {"name": "office_read", "tool_call_id": "c1", "arguments": {"path": "d.pptx"}}),
        Event(2, "tool.result", {"name": "office_read", "tool_call_id": "c1",
                                 "content": json.dumps({"document": "d.pptx", "slides": [], "guide": guide})}),
        # A call refused for what it says carries it too.
        Event(3, "tool.call", {"name": "office_edit", "tool_call_id": "c2",
                               "arguments": json.dumps({"path": "plans/Plan.DOCX", "ops": "[{"})}),
        Event(4, "tool.result", {"name": "office_edit", "tool_call_id": "c2",
                                 "content": json.dumps({"error": refused, "guide": guide})}),
        Event(5, "tool.call", {"name": "office_read", "tool_call_id": "c3", "arguments": {"path": "d.pptx"}}),
        Event(6, "tool.result", {"name": "office_read", "tool_call_id": "c3",
                                 "content": json.dumps({"document": "d.pptx", "slides": []})}),
        # Without its call in the trace, the document it names says the type.
        Event(7, "tool.result", {"name": "office_read", "content": json.dumps({"document": "b.xlsx", "guide": guide})}),
    ]
    m = metrics(events)
    assert m["office_guides"] == ["pptx", "docx", "xlsx"]
    assert m["read_office_skill"] and m["skill_views"] == []
    assert m["office_errors"] == [f"office_edit: {refused}"]


RULES = {"rules": "# Working on documents live with the user", "reference": "# office_edit for Word documents"}


def _view(n, kind, skill="office"):
    return Event(n, "tool.call", {"name": "skill_view", "arguments": {"name": skill, "file_path": f"templates/{kind}.md"}})


def _edit(n, call_id, path, answer):
    return [
        Event(n, "tool.call", {"name": "office_edit", "tool_call_id": call_id, "arguments": json.dumps({"path": path, "ops": []})}),
        Event(n + 1, "tool.result", {"name": "office_edit", "tool_call_id": call_id, "content": json.dumps(answer)}),
    ]


def test_a_template_opened_before_the_document_was_made():
    events = [
        _view(1, "docx"),
        Event(2, "tool.call", {"name": "office_read", "tool_call_id": "c0", "arguments": {"path": "out/plan.docx"}}),
        Event(3, "tool.result", {"name": "office_read", "tool_call_id": "c0",
                                 "content": json.dumps({"error": "No document at out/plan.docx."})}),
        # The call that makes it brings the guide, templates and all.
        *_edit(4, "c1", "/workspace/out/plan.docx", {"document": "out/plan.docx", "created": True, "applied": True,
                                                     "guide": {**RULES, "templates": "# Word templates"}}),
        *_edit(6, "c2", "out/plan.docx", {"document": "out/plan.docx", "applied": True}),
    ]
    m = metrics(events)
    assert m["office_guides"] == ["docx"] and m["office_guides_with_templates"] == ["docx"]
    assert m["template_viewed_before_create"] is True


def test_a_template_opened_after_the_document_was_made_or_of_another_type_is_not_before():
    made = _edit(1, "c1", "out/plan.docx", {"document": "out/plan.docx", "created": True, "applied": True,
                                            "guide": RULES})
    m = metrics([*made, _view(3, "docx")])
    assert m["template_viewed_before_create"] is False
    # A guide that came without its templates.
    assert m["office_guides"] == ["docx"] and m["office_guides_with_templates"] == []
    assert metrics([_view(0, "xlsx"), *made])["template_viewed_before_create"] is False
    # Another skill's file of that name is not the office skill's template.
    assert metrics([_view(0, "docx", skill="docx"), *made])["template_viewed_before_create"] is False


def test_the_made_document_is_known_by_its_path_when_the_editor_refused_the_call_that_made_it():
    # A first slide the editor refuses makes the deck but says nothing of it; the next call finds it there.
    events = [
        *_edit(1, "c1", "/workspace/decks/pitch.pptx", {"error": "op 0: slide 1: the title overflows its box by 120 px."}),
        _view(3, "pptx"),
        *_edit(4, "c2", "decks/pitch.pptx", {"document": "decks/pitch.pptx", "applied": True}),
    ]
    assert metrics(events)["template_viewed_before_create"] is None
    assert metrics(events, made="decks/Pitch.pptx")["template_viewed_before_create"] is False
    assert metrics([_view(0, "pptx"), *events], made="decks/pitch.pptx")["template_viewed_before_create"] is True


def test_no_document_made_of_a_type_with_templates_says_nothing_of_them():
    edited = _edit(1, "c1", "in/report.docx", {"document": "in/report.docx", "applied": True})
    assert metrics([_view(0, "docx"), *edited])["template_viewed_before_create"] is None
    form = _edit(1, "c1", "forms/new.pdf", {"document": "forms/new.pdf", "created": True, "applied": True})
    assert metrics([_view(0, "docx"), *form])["template_viewed_before_create"] is None


def test_neither_a_guide_nor_a_view_is_not_reading_the_office_skill():
    events = [
        Event(1, "tool.call", {"name": "skill_view", "arguments": {"name": "pdf"}}),
        Event(2, "tool.result", {"name": "office_read", "content": json.dumps({"document": "a.xlsx", "sheets": []})}),
    ]
    m = metrics(events)
    assert m["office_guides"] == [] and not m["read_office_skill"]


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


def _rollout(tmp_path, collected: list[tuple[str, pathlib.Path]], events: list[Event] | None = None) -> str:
    """A stored rollout whose workspace held *collected* (workspace path, file), after *events*."""
    with open(tmp_path / "events.jsonl", "w", encoding="utf-8") as fh:
        for ev in events or []:
            fh.write(json.dumps({"id": ev.id, "type": ev.type, "data": ev.data}) + "\n")
    entries = []
    for path, source in collected:
        local = tmp_path / "documents" / path
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, local)
        entries.append({"path": path, "local": f"documents/{path}", "size": local.stat().st_size,
                        "input": False, "changed": True})
    (tmp_path / "meta.json").write_text(json.dumps({"index": 1, "collected": entries}), encoding="utf-8")
    return str(tmp_path)


def _check(result: dict, name: str) -> dict:
    [found] = [c for c in result["checks"] if c["name"] == name]
    return found


def test_unsaved_document_scores_overflow_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv(RENDER_SHELL_ENV, raising=False)
    result = evaluate(by_id()["pptx-pitch"], _rollout(tmp_path, []))
    assert _check(result, OVERFLOW)["passed"] is None
    assert _check(result, "5-7 slides")["passed"] is False


def test_a_kept_render_is_measured_without_a_renderer(tmp_path, monkeypatch):
    monkeypatch.delenv(RENDER_SHELL_ENV, raising=False)
    rollout = _rollout(tmp_path, [("kpi.pptx", FIXTURES / "overflows.pptx")])
    (tmp_path / "render").mkdir()
    shutil.copyfile(FIXTURES / "overflows.pdf", tmp_path / "render" / "kpi.pdf")
    check = _check(evaluate(by_id()["pptx-kpi-chart"], rollout), OVERFLOW)
    assert check["passed"] is False and "Crowded" in check["detail"] and "pt past" in check["detail"]


def test_a_render_is_kept_for_the_next_check(tmp_path, monkeypatch):
    pdf = FIXTURES / "fits.pdf"
    code = f"import shutil, sys; shutil.copyfileobj(open({str(pdf)!r}, 'rb'), sys.stdout.buffer)"
    monkeypatch.setenv(RENDER_SHELL_ENV, f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")
    rollout = _rollout(tmp_path, [("kpi.pptx", FIXTURES / "fits.pptx")])
    assert _check(evaluate(by_id()["pptx-kpi-chart"], rollout), OVERFLOW)["passed"] is True
    assert (tmp_path / "render" / "kpi.pdf").read_bytes() == pdf.read_bytes()


def test_no_renderer_scores_overflow_unknown_and_says_why(tmp_path, monkeypatch):
    monkeypatch.delenv(RENDER_SHELL_ENV, raising=False)
    rollout = _rollout(tmp_path, [("kpi.pptx", FIXTURES / "overflows.pptx")])
    check = _check(evaluate(by_id()["pptx-kpi-chart"], rollout), OVERFLOW)
    assert check["passed"] is None and RENDER_SHELL_ENV in check["detail"]
    assert not (tmp_path / "render").exists()


def _office_edit(path: str, applied: bool = True, call_id: str = "call_1") -> list[Event]:
    """An office_edit call on *path* and its result."""
    return [
        Event(1, "tool.call", {"name": "office_edit", "arguments": {"path": path, "ops": [{"op": "slides"}]},
                               "tool_call_id": call_id}),
        Event(2, "tool.result", {"name": "office_edit", "tool_call_id": call_id, "content": json.dumps(
            {"document": path.rsplit("/", 1)[-1], "open_by": [], "applied": applied, "results": [], "flushed": True})}),
    ]


def _saved_by(source: pathlib.Path, dest: pathlib.Path, application: str) -> pathlib.Path:
    """*source* with its docProps/app.xml naming *application* as the program that saved it."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "docProps/app.xml":
                data = re.sub(rb"<Application>[^<]*</Application>",
                              f"<Application>{application}</Application>".encode(), data)
            out.writestr(item, data)
    return dest


def _scripted_deck(tmp_path) -> pathlib.Path:
    from pptx import Presentation

    (tmp_path / "made").mkdir()
    path = tmp_path / "made" / "kpi.pptx"
    Presentation().save(str(path))
    return path


def test_scripted_document_is_not_made_in_the_editor(tmp_path, monkeypatch):
    monkeypatch.delenv(RENDER_SHELL_ENV, raising=False)
    deck = _scripted_deck(tmp_path)
    assert "PowerPoint" in docs.producer(deck)
    rollout = _rollout(tmp_path, [("kpi.pptx", deck)], _office_edit("kpi.pptx"))
    check = _check(evaluate(by_id()["pptx-kpi-chart"], rollout), MADE)
    assert check["passed"] is False and docs.producer(deck) in check["detail"]


def test_editor_document_without_office_edit_fails(tmp_path, monkeypatch):
    monkeypatch.delenv(RENDER_SHELL_ENV, raising=False)
    deck = _saved_by(_scripted_deck(tmp_path), tmp_path / "made" / "editor.pptx", EDITOR)
    assert docs.producer(deck) == EDITOR
    # An applied edit of another document, and a refused one of this: neither made it.
    events = _office_edit("other.pptx") + _office_edit("kpi.pptx", applied=False, call_id="call_2")
    rollout = _rollout(tmp_path, [("kpi.pptx", deck)], events)
    check = _check(evaluate(by_id()["pptx-kpi-chart"], rollout), MADE)
    assert check["passed"] is False and "office_edit" in check["detail"]


def test_editor_document_with_office_edit_passes(tmp_path, monkeypatch):
    monkeypatch.delenv(RENDER_SHELL_ENV, raising=False)
    deck = _saved_by(_scripted_deck(tmp_path), tmp_path / "made" / "editor.pptx", EDITOR)
    rollout = _rollout(tmp_path, [("kpi.pptx", deck)], _office_edit("__WORKSPACE__/kpi.pptx"))
    assert _check(evaluate(by_id()["pptx-kpi-chart"], rollout), MADE)["passed"] is True


def test_pdf_rewritten_by_pypdf_fails(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.append(str(INPUTS / "sample.pdf"))
    (tmp_path / "made").mkdir()
    pdf = tmp_path / "made" / "service-agreement.pdf"
    writer.write(str(pdf))
    assert docs.producer(pdf) == "pypdf"
    rollout = _rollout(tmp_path, [("service-agreement.pdf", pdf)], _office_edit("service-agreement.pdf"))
    check = _check(evaluate(by_id()["pdf-annotate"], rollout), MADE)
    assert check["passed"] is False and "pypdf" in check["detail"]
