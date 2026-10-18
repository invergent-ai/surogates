"""The office skill loads as a built-in, gated on office_edit; the file skills point to it and hide where it is offered,
and the pdf skill leaves what office_edit does to a PDF to pdf-forms."""

import json
import re
from pathlib import Path

from surogates.tools.loader import ResourceLoader

SKILLS = Path(__file__).parents[1] / "skills"

# How each file skill's description starts: with the office skill, when the agent can edit live.
OFFICE_FIRST = {
    "docx": "When the office_edit tool is available and the document is in the workspace, use the office skill instead: ",
    "xlsx": "When the office_edit tool is available and the spreadsheet is a .xlsx in the workspace, use the office skill instead: ",
    "pptx": "When the office_edit tool is available and the deck is a .pptx in the workspace, use the office skill instead: ",
}
#: What office_edit does to a PDF, the pdf-forms skill does with scripts where office_edit is not offered.
PDF_FORMS_FIRST = (
    "Fill in a PDF form, annotate or redact a PDF, or add, remove or rotate its pages, when the office_edit tool is "
    "not available. "
)
#: pdf-forms' own files: the form-filling guide and the scripts it runs.
PDF_FORMS_FILES = ("FORMS.md", "LICENSE.txt") + tuple(
    f"scripts/{script}.py"
    for script in (
        "check_fillable_fields", "extract_form_field_info", "extract_form_structure", "fill_fillable_fields",
        "fill_pdf_form_with_annotations", "check_bounding_boxes", "create_validation_image", "convert_pdf_to_images",
    )
)


def test_the_office_skill_is_a_built_in_gated_on_office_edit():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    office = skills["office"]
    assert "office_edit" in office.requires_tools
    for reference in ("docx.md", "xlsx.md", "pptx.md", "pdf.md"):
        assert (SKILLS / "productivity/office/references" / reference).exists()


def test_the_office_skill_names_no_skill_its_chats_cannot_open():
    # Offered only where office_edit is, where the docx, xlsx and pptx skills are hidden: its index line and its rules
    # send the agent to none of them, and leave the pdf skill its work on a new file.
    office = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}["office"]
    for text in (office.description, office.content):
        assert not re.search(r"\b(docx|xlsx|pptx)(,? (docx|xlsx|pptx|and|or))* skills?\b", text, re.IGNORECASE), text
    assert "sandbox skill" not in office.content
    assert "The pdf skill's work (merging, splitting, encrypting) writes a new file, never over an open one." in " ".join(
        office.content.split()
    )


def test_guides_stay_small():
    # The first office call on each type carries the skill's rules, that type's reference and its templates, whole.
    office = SKILLS / "productivity/office"
    rules = re.sub(r"\A---\n.*?\n---\n", "", (office / "SKILL.md").read_text(), count=1, flags=re.DOTALL).strip()
    assert rules.startswith("# Working on documents live with the user")
    for kind in ("docx", "xlsx", "pptx", "pdf"):
        reference = (office / "references" / f"{kind}.md").read_text().strip()
        templates = office / "templates" / f"{kind}.md"
        size = len(rules) + len(reference) + (len(templates.read_text().strip()) if templates.exists() else 0)
        assert size < 40_000, (kind, size)


def test_word_templates_state_lengths():
    # A new document starts from its template; a small model writes a section to the length its intent states.
    templates = (SKILLS / "productivity/office/templates/docx.md").read_text()
    assert len(templates) <= 12_000
    for heading in ("Report", "Letter", "Meeting minutes", "Proposal", "FAQ", "How-to guide"):
        assert re.search(rf"^## {heading}$", templates, re.MULTILINE), heading
    intents = re.findall(r"<\?pending [a-z0-9_-]+: ([^?]+)\?>", templates)
    assert intents and len(intents) == templates.count("<?pending"), "an intent reads as an instruction, not a question"
    for intent in intents:
        assert re.search(r"\d+ words", intent), intent


def test_sheet_templates_total_by_formula():
    # A small model types a number where a total belongs unless the template's totals are formulas over their rows.
    templates = (SKILLS / "productivity/office/templates/xlsx.md").read_text()
    assert len(templates) <= 8_000
    sections = dict(re.findall(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", templates, re.MULTILINE | re.DOTALL))
    assert list(sections) == ["Budget", "Invoice", "Tracker"]
    calls = {}
    for name, section in sections.items():
        (calls[name],) = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", section, re.DOTALL)]
    assert any(op["op"] == "chart" for op in calls["Budget"]["ops"])
    for name, call in calls.items():
        cells = {}
        for op in call["ops"]:
            if op["op"] == "set":
                column, row = re.fullmatch(r"\w+!([A-Z])(\d+)", op["range"]).groups()
                for i, values in enumerate(op["values"]):
                    cells.update({(chr(ord(column) + j), int(row) + i): value for j, value in enumerate(values)})
        totals = [v for v in cells.values() if isinstance(v, str) and v.startswith("=SUM(")]
        assert totals, name
        # Each SUM runs over a whole list in one column, from the row under its header to its last row; the row under
        # the list is free, so a longer list moves no total, only the SUM's end.
        for column, first, last in re.findall(r"SUM\(([A-Z])(\d+):\1(\d+)\)", " ".join(totals)):
            first, last = int(first), int(last)
            assert not str(cells[(column, first - 1)]).startswith("="), (name, column, first)
            for row in range(first, last + 1):
                value = cells[(column, row)]
                assert isinstance(value, (int, float)) or value.startswith("="), (name, column, row)
            assert cells.get((column, last + 1), "") == "", (name, column, last)


def test_slide_templates_share_one_look():
    # Eight kinds of slide a deck is built from, in one palette and type scale, so slides from them sit in one deck;
    # then decks as orders of them. Small, as the slide guide they join is already the largest.
    templates = (SKILLS / "productivity/office/templates/pptx.md").read_text()
    assert len(templates) <= 14_000
    sections = dict(re.findall(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", templates, re.MULTILINE | re.DOTALL))
    kinds = ["Title", "Statement", "Big number", "Cards", "Table", "Chart", "Timeline", "Closing"]
    assert list(sections) == [*kinds, "Decks"]
    for kind in kinds:
        (call,) = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", sections[kind], re.DOTALL)]
        assert [op["op"] for op in call["ops"]] == ["slides"], kind
    assert "```" not in sections["Decks"]
    for deck in ("pitch", "onboarding", "review"):
        assert deck in sections["Decks"].lower(), deck
    assert len(set(re.findall(r"#[0-9A-Fa-f]{6}\b", templates))) <= 5, "one palette"
    sizes = {int(size) for size in re.findall(r"font-size:\s*(\d+)px", templates)}
    assert sizes and min(sizes) >= 24 and len(sizes) <= 5, sizes
    for unwanted in ("border-radius", "opacity", "rgba("):
        assert unwanted not in templates, "square corners, and text at full strength"


def test_each_file_skill_loads_and_sends_the_agent_to_the_office_skill_first():
    # A frontmatter the loader cannot parse drops its skill silently: each one must be here.
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    for name, sentence in OFFICE_FIRST.items():
        assert skills[name].description.startswith(sentence), name


def test_the_docx_xlsx_pptx_and_pdf_forms_skills_hide_where_the_office_tools_are_offered():
    # The model picks a file skill over the office skill despite the description; hiding it is what holds.
    # The pdf skill stays: it merges, splits, creates, watermarks and encrypts, which the office tools do not.
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    for name in ("docx", "xlsx", "pptx", "pdf-forms"):
        assert skills[name].fallback_for_tools == ["office_edit"], name
    assert not skills["pdf"].fallback_for_tools


def test_the_pdf_skill_leaves_what_office_edit_does_to_pdf_forms():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    assert skills["pdf-forms"].description.startswith(PDF_FORMS_FIRST)
    # Shown in every chat: its description names nothing office_edit does, so it never draws the model from it.
    description = skills["pdf"].description
    assert not re.search(r"office_edit|\bforms?\b|rotat|annotat|redact", description, re.IGNORECASE), description
    # Its body teaches none of it, and sends that work on by where the chat is.
    body = skills["pdf"].content
    assert "fill, annotate, redact or rotate: office_edit where the chat has it, else the pdf-forms skill" in body
    for teaching in ("FORMS.md", ".rotate(", "--rotate", "1east"):
        assert teaching not in body, teaching


def test_the_form_guide_and_its_scripts_live_in_pdf_forms():
    pdf, forms = SKILLS / "productivity/pdf", SKILLS / "productivity/pdf-forms"
    for path in PDF_FORMS_FILES:
        assert (forms / path).is_file(), path
        assert path == "LICENSE.txt" or not (pdf / path).exists(), path
    # What stays: the reference, the license and the watermark script.
    assert sorted(p.relative_to(pdf).as_posix() for p in pdf.rglob("*") if p.is_file()) == [
        "LICENSE.txt", "REFERENCE.md", "SKILL.md", "scripts/watermark.py",
    ]
