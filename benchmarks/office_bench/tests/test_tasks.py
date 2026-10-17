"""The suite's checks against documents built here: a correct result passes every check, the untouched input fails.

This is what makes a check trustworthy: it passes the document a careful person would produce, and it catches the
document the agent left unchanged. The fixtures' contents the edit checks rely on are pinned too.
"""
import copy
import os

import pytest

from obench import docs
from obench.checks import Ctx
from obench.tasks import FIXTURES, TASKS, by_id

T = by_id()


def run_checks(task_id: str, path: str, inputs_changed: list[str] | None = None):
    task = T[task_id]
    inp = task.original_input
    original = None
    if inp is not None:
        src = str(inp.source())
        if inp.derive:
            derived = path + ".input" + os.path.splitext(path)[1]
            inp.derive(src, derived)
            src = derived
        original = docs.read(src)
    ctx = Ctx(original=original, final_message="", inputs_changed=inputs_changed or [])
    doc = docs.read(path)
    return [c.run(doc, ctx) for c in task.checks]


def failures(results):
    return [(r.name, r.detail) for r in results if r.passed is False]


# --------------------------------------------------------------------------- the suite itself


def test_suite_shape():
    ids = [t.id for t in TASKS]
    assert len(ids) == len(set(ids))
    assert 30 <= len(TASKS) <= 36
    assert sum(t.mode == "create" for t in TASKS) > sum(t.mode == "edit" for t in TASKS)
    assert {t.kind for t in TASKS} == {"docx", "xlsx", "pptx", "pdf"}
    pilot = [t for t in TASKS if t.pilot]
    assert len(pilot) == 6 and {t.kind for t in pilot} == {"docx", "xlsx", "pptx", "pdf"}
    assert sum(t.mode == "create" for t in pilot) >= 3


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.id)
def test_task_is_well_formed(task):
    assert docs.kind_of(task.target) == task.kind
    assert task.target in task.prompt, "the person names the file"
    assert len(task.checks) >= 3
    for inp in task.inputs:
        assert inp.source().is_file(), inp.fixture
        assert inp.name in task.prompt
    if task.mode == "edit":
        assert task.original_input is not None


def test_fixture_contents_are_what_the_checks_assume():
    d = docs.read(str(FIXTURES / "sample.docx"))
    texts = [p.text for p in d.paragraphs]
    assert "North grew fastest, then the east." in texts
    assert "Hiring could slip a quarter." in texts
    assert d.tables[0].rows == [["Region", "Growth"], ["North", "12%"]]
    assert [p.text for p in d.paragraphs if p.list_kind == "number"] == ["Hire two engineers", "Open the Lisbon office"]
    x = docs.read(str(FIXTURES / "sample.xlsx"))
    assert x.sheets == ["Sales", "Notes"] and x.raw("Sales", "D3") == "=B3+C3"
    p = docs.read(str(FIXTURES / "sample.pptx"))
    assert [s.title for s in p.slides] == ["Quarterly review", "Next quarter"]
    assert p.slides[1].notes == "Mention the lease."
    f = docs.read(str(FIXTURES / "sample.pdf"))
    assert set(f.fields) == {"client", "signed"} and len(f.pages) == 2
    assert f.y_of("thirty days") == (1, 712.0)


@pytest.mark.parametrize("task", [t for t in TASKS if t.mode == "edit"], ids=lambda t: t.id)
def test_untouched_input_fails_the_task(task, tmp_path):
    """Handing back the input unchanged must fail; what it keeps must pass."""
    src = str(task.original_input.source())
    path = str(tmp_path / task.target)
    if task.original_input.derive:
        task.original_input.derive(src, path)
    else:
        with open(src, "rb") as a, open(path, "wb") as b:
            b.write(a.read())
    results = run_checks(task.id, path)
    assert failures(results), "an unchanged document passed every check"
    assert not [r for r in results if r.passed is None and "crashed" in r.detail]


# --------------------------------------------------------------------------- Word, done right


def _docx(tmp_path, name="quarterly-report.docx"):
    import docx

    return docx.Document(str(FIXTURES / "sample.docx")), str(tmp_path / name)


def _para(d, text):
    return next(p for p in d.paragraphs if p.text == text)


def test_docx_reword_done_right(tmp_path):
    d, path = _docx(tmp_path)
    _para(d, "North grew fastest, then the east.").runs[0].text = "The east grew fastest, then the north."
    d.save(path)
    assert failures(run_checks("docx-reword", path)) == []


def test_docx_reword_that_loses_the_bold_fails(tmp_path):
    d, path = _docx(tmp_path)
    _para(d, "North grew fastest, then the east.").runs[0].text = "The east grew fastest, then the north."
    for r in d.paragraphs[1].runs:
        r.bold = False
    d.save(path)
    assert [n for n, _ in failures(run_checks("docx-reword", path))] == ["'revenue target' is bold"]


def test_docx_fix_table_done_right(tmp_path):
    d, path = _docx(tmp_path)
    d.tables[0].cell(1, 1).text = "14%"
    d.save(path)
    assert failures(run_checks("docx-fix-table", path)) == []


def test_docx_bold_done_right(tmp_path):
    d, path = _docx(tmp_path)
    _para(d, "Hiring could slip a quarter.").runs[0].bold = True
    d.save(path)
    assert failures(run_checks("docx-bold", path)) == []


def test_docx_continue_list_done_right_and_restarted(tmp_path):
    d, path = _docx(tmp_path)
    last = _para(d, "Open the Lisbon office")
    new = copy.deepcopy(last._p)
    last._p.addnext(new)
    from docx.text.paragraph import Paragraph

    Paragraph(new, last._parent).runs[0].text = "Renew the support contract"
    d.save(path)
    assert failures(run_checks("docx-continue-list", path)) == []

    # The same item in a list of its own (another numId that restarts) is caught.
    from docx.oxml import parse_xml

    numbering = d.part.numbering_part.element
    abstract = numbering.xpath("./w:num[@w:numId='5']/w:abstractNumId/@w:val")[0]
    numbering.append(parse_xml(
        '<w:num xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" w:numId="99">'
        f'<w:abstractNumId w:val="{abstract}"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
    ))
    ppr = new.get_or_add_pPr()
    numpr = parse_xml('<w:numPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                      '<w:ilvl w:val="0"/><w:numId w:val="99"/></w:numPr>')
    ppr.append(numpr)
    d.save(path)
    fails = failures(run_checks("docx-continue-list", path))
    assert fails and "1. Renew the support contract" in fails[0][1]


def test_docx_add_section_done_right(tmp_path):
    d, path = _docx(tmp_path)
    d.add_heading("Next steps", level=2)
    d.add_paragraph("Next quarter we hire the two engineers, sign the Lisbon lease and review regional targets "
                    "with each lead by the end of the first month.")
    d.save(path)
    assert failures(run_checks("docx-add-section", path)) == []


def test_docx_add_section_as_bold_text_is_not_a_heading(tmp_path):
    d, path = _docx(tmp_path)
    d.add_paragraph().add_run("Next steps").bold = True
    d.add_paragraph("Next quarter we hire the two engineers, sign the Lisbon lease and review regional targets.")
    d.save(path)
    assert "not a heading" in failures(run_checks("docx-add-section", path))[0][1]


def test_docx_letter_done_right(tmp_path):
    import docx

    d = docx.Document()
    d.add_paragraph("Northwind Lda")
    d.add_paragraph("Mr Daniel Costa, Costa Properties, Rua Augusta 112, Lisbon")
    d.add_paragraph("Dear Mr Costa,")
    d.add_paragraph("We are writing to give notice that we are ending the lease on our office at Rua do Ouro 45 on "
                    "31 January 2027, as the 90-day notice period in the contract requires. Please arrange the final "
                    "inspection in the week of 18 January. We also ask you to return our deposit of €4,500 to the "
                    "account you have on file. Thank you for your help over the years.")
    d.add_paragraph("Kind regards,")
    d.add_paragraph("Ana Ribeiro")
    d.add_paragraph("Operations Manager, Northwind Lda")
    path = str(tmp_path / "lease-termination-letter.docx")
    d.save(path)
    assert failures(run_checks("docx-letter", path)) == []


# --------------------------------------------------------------------------- Spreadsheets, done right


def _sample_xlsx():
    import openpyxl

    return openpyxl.load_workbook(str(FIXTURES / "sample.xlsx"))


def test_xlsx_add_column_done_right(tmp_path):
    book = _sample_xlsx()
    ws = book["Sales"]
    ws["E1"] = "Growth"
    for r in (2, 3, 4):
        ws[f"E{r}"] = f"=(C{r}-B{r})/B{r}"
        ws[f"E{r}"].number_format = "0.0%"
    path = str(tmp_path / "sales.xlsx")
    book.save(path)
    results = run_checks("xlsx-add-column", path)
    assert failures(results) == []


def test_xlsx_add_column_typed_numbers_fail(tmp_path):
    book = _sample_xlsx()
    ws = book["Sales"]
    ws["E1"] = "Growth"
    for r, v in ((2, 0.1667), (3, 0.2222), (4, -0.0714)):
        ws[f"E{r}"] = v
    path = str(tmp_path / "sales.xlsx")
    book.save(path)
    assert "not all formulas" in failures(run_checks("xlsx-add-column", path))[0][1]


def test_xlsx_fix_formula_done_right(tmp_path):
    book = _sample_xlsx()
    path = str(tmp_path / "sales.xlsx")
    book.save(path)
    assert failures(run_checks("xlsx-fix-formula", path)) == []


def test_xlsx_rename_sheet_done_right(tmp_path):
    book = _sample_xlsx()
    book["Notes"].title = "Read me"
    path = str(tmp_path / "sales.xlsx")
    book.save(path)
    assert failures(run_checks("xlsx-rename-sheet", path)) == []


def test_xlsx_budget_done_right(tmp_path):
    import openpyxl
    from openpyxl.chart import PieChart, Reference

    book = openpyxl.Workbook()
    ws = book.active
    rows = [("Income", None), ("Salary", 3200), ("Freelance", 650), ("Total income", "=SUM(B2:B3)"), ("", None),
            ("Expenses", None), ("Rent", 1150), ("Groceries", 420), ("Utilities", 180), ("Transport", 140),
            ("Insurance", 95), ("Savings", 500), ("Fun", 200), ("Total expenses", "=SUM(B7:B13)"), ("", None),
            ("Left over", "=B4-B14")]
    for i, (a, b) in enumerate(rows, start=1):
        ws[f"A{i}"], ws[f"B{i}"] = a, b
        ws[f"B{i}"].number_format = '"€"#,##0.00'
    chart = PieChart()
    chart.add_data(Reference(ws, min_col=2, min_row=7, max_row=13))
    chart.set_categories(Reference(ws, min_col=1, min_row=7, max_row=13))
    ws.add_chart(chart, "D2")
    path = str(tmp_path / "budget.xlsx")
    book.save(path)
    results = run_checks("xlsx-budget", path)
    assert failures(results) == []
    # openpyxl stores no computed values: the consistency check says it cannot tell.
    assert [r.passed for r in results if r.name.startswith("stored values")] == [None]


def test_xlsx_budget_hardcoded_totals_fail(tmp_path):
    import openpyxl

    book = openpyxl.Workbook()
    ws = book.active
    for i, v in enumerate([3200, 650, 3850, 1150, 420, 180, 140, 95, 500, 200, 2685, 1165], start=1):
        ws[f"B{i}"] = v
    path = str(tmp_path / "budget.xlsx")
    book.save(path)
    details = dict(failures(run_checks("xlsx-budget", path)))
    assert "typed in as a number" in details["a formula computes total expenses = 2685"]


def test_xlsx_sales_table_done_right(tmp_path):
    import openpyxl
    from openpyxl.styles import Font

    from obench.tasks import SALES_ROWS

    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["City", "Units", "Revenue"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for city, (units, revenue) in sorted(SALES_ROWS.items(), key=lambda kv: -kv[1][1]):
        ws.append([city, units, revenue])
        ws.cell(ws.max_row, 3).number_format = "#,##0"
    path = str(tmp_path / "sales-2026.xlsx")
    book.save(path)
    assert failures(run_checks("xlsx-sales-table", path)) == []


def test_xlsx_multi_sheet_and_dates_done_right(tmp_path):
    import datetime as dt

    import openpyxl

    book = openpyxl.Workbook()
    book.active.title = "Jan"
    months = {"Jan": (120, 98, 140), "Feb": (132, 110, 138), "Mar": (128, 105, 150)}
    for i, (m, vals) in enumerate(months.items()):
        ws = book[m] if i == 0 else book.create_sheet(m)
        for row, (name, v) in enumerate(zip(("Ana", "Ben", "Carla"), vals), start=2):
            ws[f"A{row}"], ws[f"B{row}"] = name, v
    s = book.create_sheet("Summary")
    for row, name in enumerate(("Ana", "Ben", "Carla"), start=2):
        s[f"A{row}"], s[f"B{row}"] = name, f"=Jan!B{row}+Feb!B{row}+'Mar'!B{row}"
    path = str(tmp_path / "team-hours.xlsx")
    book.save(path)
    assert failures(run_checks("xlsx-multi-sheet", path)) == []

    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Task", "Owner", "Start", "End", "Days"])
    plan = [("Copy final", "Inês", 3, 7), ("Landing page", "Rui", 5, 14), ("Email campaign", "Ana", 10, 21),
            ("Press kit", "Inês", 12, 19), ("Launch event", "Marta", 24, 24)]
    for i, (t, o, a, b) in enumerate(plan, start=2):
        ws.append([t, o, dt.datetime(2026, 11, a), dt.datetime(2026, 11, b), f"=D{i}-C{i}+1"])
    path = str(tmp_path / "launch-plan.xlsx")
    book.save(path)
    assert failures(run_checks("xlsx-dates", path)) == []


def test_xlsx_invoice_done_right(tmp_path):
    import openpyxl

    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Invoice 1043", "Acme Corp"])
    ws.append(["Item", "Qty", "Price", "Total"])
    for i, (item, q, p) in enumerate([("Website redesign", 1, 4800), ("Hosting", 12, 35), ("Support", 18, 85)], start=3):
        ws.append([item, q, p, f"=B{i}*C{i}"])
    ws.append(["Subtotal", None, None, "=SUM(D3:D5)"])
    ws.append(["VAT 23%", None, None, "=ROUND(D6*23%,2)"])
    ws.append(["Total due", None, None, "=D6+D7"])
    path = str(tmp_path / "invoice-1043.xlsx")
    book.save(path)
    assert failures(run_checks("xlsx-invoice", path)) == []


# --------------------------------------------------------------------------- Decks, done right


def _pptx():
    from pptx import Presentation

    return Presentation(str(FIXTURES / "sample.pptx"))


def test_pptx_add_bullet_done_right_and_in_a_separate_box(tmp_path):
    deck = _pptx()
    deck.slides[0].placeholders[1].text_frame.add_paragraph().text = "Costs came in 4% under budget"
    path = str(tmp_path / "quarterly-review.pptx")
    deck.save(path)
    assert failures(run_checks("pptx-add-bullet", path)) == []

    from pptx.util import Inches

    deck = _pptx()
    deck.slides[0].shapes.add_textbox(Inches(1), Inches(6), Inches(6), Inches(0.6)).text_frame.text = (
        "Costs came in 4% under budget")
    deck.save(path)
    assert "separate box" in failures(run_checks("pptx-add-bullet", path))[0][1]


def test_pptx_change_title_and_notes_done_right(tmp_path):
    deck = _pptx()
    deck.slides[1].shapes.title.text = "Q3 plan"
    path = str(tmp_path / "quarterly-review.pptx")
    deck.save(path)
    assert failures(run_checks("pptx-change-title", path)) == []

    deck = _pptx()
    deck.slides[0].notes_slide.notes_text_frame.text = (
        "Thank the regional leads. Hiring slipped because two offers were declined.")
    deck.save(path)
    assert failures(run_checks("pptx-notes", path)) == []


def test_pptx_reorder_done_right(tmp_path):
    deck = _pptx()
    ids = deck.slides._sldIdLst
    first = ids[0]
    ids.remove(first)
    ids.append(first)
    path = str(tmp_path / "quarterly-review.pptx")
    deck.save(path)
    assert failures(run_checks("pptx-reorder", path)) == []


def test_pptx_table_slide_done_right(tmp_path):
    from pptx.util import Inches

    deck = _pptx()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Revenue by region"
    table = slide.shapes.add_table(4, 3, Inches(1), Inches(2), Inches(8), Inches(3)).table
    for r, row in enumerate([["Region", "Q1", "Q2"], ["North", "120", "140"], ["East", "90", "110"], ["South", "70", "65"]]):
        for c, v in enumerate(row):
            table.cell(r, c).text = v
    path = str(tmp_path / "quarterly-review.pptx")
    deck.save(path)
    assert failures(run_checks("pptx-table-slide", path)) == []


def test_pptx_overflow_is_caught(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(0.8))
    box.text_frame.word_wrap = True
    box.text_frame.text = "A sentence far too long for this small box, set large, so it runs over its bottom edge " * 2
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(32)
    path = str(tmp_path / "x.pptx")
    deck.save(path)
    from obench.checks import p_no_overflow

    passed, detail = p_no_overflow().fn(docs.read(path), Ctx())
    assert passed is False and "tall" in detail


# --------------------------------------------------------------------------- PDFs, done right


def test_pdf_fill_done_right(tmp_path):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(str(FIXTURES / "sample.pdf")))
    writer.update_page_form_field_values(writer.pages[0], {"client": "Northwind Lda", "signed": "/Yes"},
                                         auto_regenerate=False)
    path = str(tmp_path / "service-agreement.pdf")
    writer.write(path)
    assert failures(run_checks("pdf-fill", path)) == []


def test_pdf_annotate_done_right_and_far_away(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import FreeText

    def write(rect):
        writer = PdfWriter(clone_from=PdfReader(str(FIXTURES / "sample.pdf")))
        writer.add_annotation(0, FreeText(text="Finance wants 45 days, check with legal.", rect=rect))
        path = str(tmp_path / "service-agreement.pdf")
        writer.write(path)
        return path

    assert failures(run_checks("pdf-annotate", write((380, 700, 560, 730)))) == []
    fails = failures(run_checks("pdf-annotate", write((60, 100, 260, 140))))
    assert fails and "the line is at y=712" in fails[0][1]
