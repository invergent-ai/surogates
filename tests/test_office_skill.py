"""The office skill loads as a built-in, gated on office_edit, and the docx skill points to it."""

from pathlib import Path

from surogates.tools.loader import ResourceLoader

SKILLS = Path(__file__).parents[1] / "skills"


def test_the_office_skill_is_a_built_in_gated_on_office_edit():
    skills = {s.name: s for s in ResourceLoader()._load_skills_from_dir(str(SKILLS), "platform")}
    office = skills["office"]
    assert "office_edit" in office.requires_tools
    assert (SKILLS / "productivity/office/references/word.md").exists()
    assert "office" in skills["docx"].description
