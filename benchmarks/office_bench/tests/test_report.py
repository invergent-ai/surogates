"""The run report: what each rollout shows, and the run's health beside the score."""
import json

from obench.client import Event
from obench.evaluate import metrics
from obench.report import render

RULES = {"rules": "# Working on documents live with the user", "reference": "# office_edit for Word documents"}


def _made(task_id: str, *, templates: bool, viewed_first: bool) -> dict:
    """A rollout that made plan.docx, its guide with or without the templates, the template opened first or not."""
    view = Event(0, "tool.call", {"name": "skill_view", "arguments": {"name": "office", "file_path": "templates/docx.md"}})
    guide = {**RULES, "templates": "# Word templates"} if templates else RULES
    events = [
        Event(1, "tool.call", {"name": "office_edit", "tool_call_id": "c1", "arguments": {"path": "plan.docx", "ops": []}}),
        Event(2, "tool.result", {"name": "office_edit", "tool_call_id": "c1", "content": json.dumps(
            {"document": "plan.docx", "created": True, "applied": True, "guide": guide})}),
    ]
    return {
        "task_id": task_id, "index": 1, "kind": "docx", "mode": "create", "session_id": "s", "channel": "studio",
        "terminal_status": "completed", "error": None, "wall_clock_s": 60, "checks": [], "passed": 3, "failed": 0,
        "unknown": 0, "metrics": metrics([view, *events] if viewed_first else [*events, view]),
        "claims": {"contradicted": [], "claims_success_despite_failures": False},
    }


def _row(text: str, starts: str) -> list[str]:
    """The cells of the table row that starts with *starts*."""
    (line,) = [line for line in text.splitlines() if line.startswith(f"| {starts}")]
    return [cell.strip() for cell in line.strip("|").split("|")]


def test_the_report_says_whether_templates_reached_the_model_before_its_first_edit_on_the_document():
    old = _made("docx-old", templates=True, viewed_first=True)
    # Scored before these were counted: its checks.json has neither.
    del old["metrics"]["office_guides_with_templates"], old["metrics"]["template_viewed_before_create"]
    text = render([
        _made("docx-a", templates=True, viewed_first=True),
        _made("docx-b", templates=False, viewed_first=False),
        old,
    ])
    header = _row(text, "Task |")
    at = header.index("Guides with templates")
    assert header[at + 1] == "Template opened before first edit"
    assert _row(text, "`docx-a`")[at:at + 2] == ["1/1", "yes"]
    assert _row(text, "`docx-b`")[at:at + 2] == ["0/1", "no"]
    assert _row(text, "`docx-old`")[at:at + 2] == ["-", "-"]
    assert "- guides on a Word document, spreadsheet or deck that carried their templates: 1/2" in text
    assert ("- documents made whose type's template was opened before their first edit (skill_view of office "
            "templates/<kind>.md before the first office_edit on them): 1/2") in text


def test_the_report_says_which_sessions_asked_the_user_and_what():
    answered = {"tool_call_id": "a1", "questions": ["What should the first week slide show?"], "answered": True}
    unanswered = {"tool_call_id": "a3", "questions": ["Which tools?", "Who leads?"], "answered": False,
                  "why": "asked again after 2 answers"}
    once = {**_made("pptx-once", templates=True, viewed_first=True), "asks": [answered]}
    waiting = {**_made("pptx-waiting", templates=True, viewed_first=True), "asks": [answered, answered, unanswered],
               "terminal_status": "waiting_on_user"}
    never = {**_made("pptx-never", templates=True, viewed_first=True), "asks": []}
    old = _made("pptx-old", templates=True, viewed_first=True)  # run before the runner answered questions
    text = render([once, waiting, never, old])
    at = _row(text, "Task |").index("Asked the user")
    assert _row(text, "`pptx-once`")[at] == "1 (1 answered)"
    assert _row(text, "`pptx-waiting`")[at] == "3 (2 answered)"
    assert _row(text, "`pptx-never`")[at] == "0"
    assert _row(text, "`pptx-old`")[at] == "-"
    assert "- sessions that asked the user: 2 (1 ended waiting on the user, past the runner's answers)" in text
    questions = text.split("## Questions to the user", 1)[1].split("\n## ", 1)[0]
    assert "- `pptx-once` r1, answered: What should the first week slide show?" in questions
    assert ("- `pptx-waiting` r1, not answered (asked again after 2 answers): Which tools? / Who leads?"
            in questions)


def test_a_run_where_no_session_asked_says_so():
    text = render([_made("docx-a", templates=True, viewed_first=True)])
    assert "- sessions that asked the user: 0 (0 ended waiting on the user, past the runner's answers)" in text
    assert text.split("## Questions to the user", 1)[1].split("\n## ", 1)[0].strip() == "None."
