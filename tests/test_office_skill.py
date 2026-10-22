"""The office skill loads as a built-in, gated on office_edit; the file skills point to it and hide where it is offered."""

from pathlib import Path

from surogates.tools.loader import ResourceLoader

SKILLS = Path(__file__).parents[1] / "skills"

# How each file skill's description starts: with the office skill, when the agent can edit live.
OFFICE_FIRST = {
    "docx": "When the office_edit tool is available and the document is in the workspace, use the office skill instead: ",
    "xlsx": "When the office_edit tool is available and the spreadsheet is a .xlsx in the workspace, use the office skill instead: ",
    "pptx": "When the office_edit tool is available and the deck is a .pptx in the workspace, use the office skill instead: ",
}


def test_the_office_skill_is_a_built_in_gated_on_office_edit():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    office = skills["office"]
    assert "office_edit" in office.requires_tools
    for reference in ("docx.md", "xlsx.md", "pptx.md", "pdf.md"):
        assert (SKILLS / "productivity/office/references" / reference).exists()


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
