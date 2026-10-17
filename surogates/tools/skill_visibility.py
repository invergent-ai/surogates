"""Which fallback skills a session does not see.

A skill with ``fallback_for_tools`` stands in for those tools where they are not offered: the docx, xlsx, pptx and
pdf-forms skills make documents with scripts, which office_edit does live in the user's document pane. Where every
tool it stands in for is offered, the skill is hidden: from the prompt's skill index, from ``skills_list`` and from
``skill_view``, so the model never builds with scripts what the chat edits live.

The prompt knows the session's toolset and hides by it. ``skills_list`` and ``skill_view`` reach the skills through
the api, which has only the session's row, so they hide by the tools the row tells: the office tools.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from surogates.config import load_settings


def offered_fallback_tools(channel: str | None, config: dict[str, Any] | None) -> frozenset[str]:
    """The tools a fallback skill can stand in for that a session on *channel* with *config* is sent: the office
    tools where the agent edits documents live and the session is offered them, else none."""
    # The guard reaches the prompt builder, which imports this module.
    from surogates.office.guard import OFFICE_TOOLS, office_tools_offered

    if not load_settings().office.agent_editing:
        return frozenset()
    if not office_tools_offered(SimpleNamespace(channel=channel, config=config or {})):
        return frozenset()
    return OFFICE_TOOLS


def fallback_hidden(fallback_for_tools: Sequence[str], offered: frozenset[str]) -> bool:
    """Whether a skill standing in for *fallback_for_tools* is hidden from a session *offered* those tools: every one
    of them is offered. A skill that stands in for nothing is never hidden."""
    return bool(fallback_for_tools) and all(tool in offered for tool in fallback_for_tools)


def fallback_refusal(name: str) -> str:
    """What ``skill_view`` answers for a fallback skill the session does not see."""
    return (
        f"{name} is for chats without the office tools: this chat makes and edits documents live with office_edit "
        "(the office skill)."
    )
