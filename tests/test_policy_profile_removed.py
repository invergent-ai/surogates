"""`policy_profile` is gone, and a definition that still names it still loads.

The field promised to narrow a sub-agent's policy below its parent's. Nothing
ever resolved a profile name, so it narrowed nothing — and the intended model
is that a sub-agent carries the SAME policy as its parent, which is exactly
what already happens (a child inherits the parent's agent_id and therefore its
governance blob).

Tool restriction, the part people actually wanted, was never affected: it
comes from `agent_def.tools` -> `config["allowed_tools"]`, which is enforced by
never handing the schema to the model.

Existing agent definitions in the wild may still carry `policy_profile:` in
their frontmatter. Those must keep loading — the key is ignored, never
rejected.
"""

from __future__ import annotations

from surogates.tools.loader import AgentDef, _parse_agent_frontmatter


def test_a_definition_that_still_names_it_loads():
    body = (
        "---\n"
        "name: auditor\n"
        "description: read things\n"
        "policy_profile: read_only\n"
        "tools: read_file, search_files\n"
        "---\n"
        "You audit.\n"
    )
    parsed = _parse_agent_frontmatter(body, "auditor")
    assert parsed is not None
    assert parsed.get("tools") == ["read_file", "search_files"]


def test_the_parsed_result_no_longer_carries_it():
    body = (
        "---\nname: auditor\ndescription: d\npolicy_profile: read_only\n---\nx\n"
    )
    assert "policy_profile" not in (_parse_agent_frontmatter(body, "auditor") or {})


def test_the_field_is_gone_from_the_model():
    assert not hasattr(
        AgentDef(name="a", description="d", system_prompt="s", source="platform"),
        "policy_profile",
    )


def test_nothing_stamps_it_into_a_session_config():
    """Four spawn paths used to write it. None may now."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "surogates"
    writers = [
        p for p in root.rglob("*.py")
        if "policy_profile" in p.read_text()
    ]
    assert writers == [], f"still referenced in: {[str(p) for p in writers]}"
