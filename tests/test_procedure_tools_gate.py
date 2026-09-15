"""skill_step is offered exactly when a graph-backed skill is in the catalog."""

from types import SimpleNamespace

from surogates.harness.procedure_tools import gate_skill_step

ALL = {"terminal", "read_file", "skill_step", "todo"}


def _skill(has_graph):
    return SimpleNamespace(name="proc", has_graph=has_graph)


def test_no_graph_skill_removes_the_tool_from_an_open_filter():
    assert gate_skill_step(None, all_tools=ALL, skills=[_skill(False)]) == ALL - {"skill_step"}


def test_no_graph_skill_removes_the_tool_from_an_allow_list():
    assert gate_skill_step({"terminal", "skill_step"}, all_tools=ALL, skills=[]) == {"terminal"}


def test_graph_skill_adds_the_tool_even_to_an_allow_list():
    assert gate_skill_step({"terminal"}, all_tools=ALL, skills=[_skill(True)]) == {"terminal", "skill_step"}


def test_graph_skill_keeps_an_open_filter_open():
    assert gate_skill_step(None, all_tools=ALL, skills=[_skill(True)]) is None


def test_tool_not_registered_is_never_added():
    assert gate_skill_step({"terminal"}, all_tools={"terminal"}, skills=[_skill(True)]) == {"terminal"}
