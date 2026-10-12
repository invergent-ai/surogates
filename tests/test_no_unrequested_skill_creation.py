"""Nothing in the prompt or the tool descriptions tells the agent to create skills on its own.

The skills guidance said to save a skill after any task of five or more tool calls, the
memory tool and the skill_manage tool echoed it, and a PROD docx session ended a routine
NDA by offering to save its page-fitting loop as a skill, a loop built on a LibreOffice
"race" it had misdiagnosed.  Skills are made when the user asks; fixing a wrong skill stays.
"""

from __future__ import annotations

from pathlib import Path

import surogates.harness
from surogates.memory.builtin import MEMORY_TOOL_SCHEMA
from surogates.tools.builtin import memory as memory_tool
from surogates.tools.builtin.skill_manager import SCHEMA as SKILL_MANAGE_SCHEMA
from surogates.tools.registry import ToolRegistry

_GUIDANCE = Path(surogates.harness.__file__).with_name("prompts") / "guidance"
_NUDGES = ("5+", "offer to save", "save it as a skill", "save the approach as a skill")


def _memory_tool_description() -> str:
    registry = ToolRegistry()
    memory_tool.register(registry)
    return registry.get_schemas({"memory"})[0]["function"]["description"]


def test_no_text_asks_the_agent_to_create_skills_unprompted():
    texts = {
        "guidance/skills.md": (_GUIDANCE / "skills.md").read_text(),
        "guidance/memory.md": (_GUIDANCE / "memory.md").read_text(),
        "memory tool": _memory_tool_description(),
        "memory schema": MEMORY_TOOL_SCHEMA["description"],
        "skill_manage tool": SKILL_MANAGE_SCHEMA.description,
    }
    found = [(where, nudge) for where, text in texts.items() for nudge in _NUDGES if nudge in text]
    assert found == []


def test_fixing_a_wrong_skill_is_still_asked_for():
    assert "patch it immediately" in SKILL_MANAGE_SCHEMA.description
    assert "patch" in (_GUIDANCE / "skills.md").read_text()
