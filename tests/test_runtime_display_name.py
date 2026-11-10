import pytest

from surogates.runtime.resolver import build_agent_runtime_context

BASE = {"agent_id": "a", "org_id": "o", "project_id": "p", "enabled": True, "version": 1, "storage_key_prefix": ""}


@pytest.mark.parametrize(
    ("given", "shown"),
    [
        ("  Support Agent ", "Support Agent"),
        ("Agent Admin‮strativ", "Agent Adminstrativ"),
        ("Sup\u00adport", "Support"),
        ("Support\nAgent", "Support Agent"),
        ("A" * 100, "A" * 79 + "…"),
        ("   ", None),
        (None, None),
        (7, None),
        ({"name": "x"}, None),
    ],
)
def test_the_agent_is_named_on_one_line_with_nothing_hidden(given, shown):
    assert build_agent_runtime_context({**BASE, "display_name": given}).display_name == shown
