"""The office skill loads as a built-in, gated on office_edit; the file skills point to it and hide where it is offered,
and the pdf skill leaves what office_edit does to a PDF to pdf-forms."""

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
    # The first office call on each type carries the skill's rules and that type's reference, whole.
    rules = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}["office"].content
    for kind in ("docx", "xlsx", "pptx", "pdf"):
        reference = (SKILLS / "productivity/office/references" / f"{kind}.md").read_text()
        assert len(rules) + len(reference) < 40_000, kind


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
