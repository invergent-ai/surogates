from surogates.tools.builtin.skill_validation import (
    GRAPH_FILE,
    is_graph_file,
    validate_file_path,
)


def test_graph_file_constant():
    assert GRAPH_FILE == "SKILL.graph.json"


def test_graph_file_rejected_at_root_like_any_other_file():
    # Nothing in the harness writes the graph file -- ops writes it through
    # the Hub client -- so an agent authoring one via write_file must be
    # rejected the same as any other root-level file.
    assert validate_file_path(GRAPH_FILE) is not None


def test_other_root_files_stay_rejected():
    assert validate_file_path("SKILL.md") is not None
    assert validate_file_path("notes.json") is not None
    assert validate_file_path("nested/SKILL.graph.json") is not None


def test_is_graph_file_matches_root_variants_case_insensitively():
    assert is_graph_file("SKILL.graph.json") is True
    assert is_graph_file("./SKILL.graph.json") is True
    assert is_graph_file("/SKILL.graph.json") is True
    assert is_graph_file("Skill.Graph.JSON") is True


def test_is_graph_file_false_for_subdirectory_file():
    assert is_graph_file("references/SKILL.graph.json") is False
