from surogates.tools.builtin.skill_validation import GRAPH_FILE, validate_file_path


def test_graph_file_is_the_one_allowed_root_file():
    assert GRAPH_FILE == "SKILL.graph.json"
    assert validate_file_path(GRAPH_FILE) is None


def test_other_root_files_stay_rejected():
    assert validate_file_path("SKILL.md") is not None
    assert validate_file_path("notes.json") is not None
    assert validate_file_path("nested/SKILL.graph.json") is not None
