"""Which fallback skills a session does not see.

A skill with ``fallback_for_tools`` stands in for those tools where they are not offered: the docx, xlsx, pptx and
pdf-forms skills make documents with scripts, which office_edit does live in the user's document pane. Where every
tool it stands in for is offered, the skill is hidden: from the prompt's skill index, from ``skills_list`` and from
``skill_view``, so the model never builds with scripts what the chat edits live.

The prompt knows the session's toolset and hides by it. ``skills_list`` and ``skill_view`` reach the skills through
the api, which has only the session's row, so they hide by the tools the row tells: the office tools.

A skill that names, or ``requires_tools``, a tool the session can never be sent (its ``excluded_tools``, a tool an
``allowed_tools`` list leaves out, one its kind never gets) is hidden too: from the skill index, from ``skills_list``
and from ``skill_view``.  The model read ``read_file`` in a skill's text and called it, and was refused "Unknown
tool".  The prompt hides by its narrowed toolset; the worker sets :data:`skill_scope` for the turn's tool calls, so
the skill tools leave the same skills out whichever way they reach the skills.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
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


@dataclass(frozen=True)
class SkillScope:
    """The skills a session's skill tools leave out this turn: those that name a tool it cannot be sent."""

    hidden: frozenset[str] = frozenset()


#: Set by the worker for the turn's tool calls (``AgentHarness``); outside a turn it hides nothing.
skill_scope: ContextVar[SkillScope] = ContextVar("surogates_skill_scope", default=SkillScope())


def _tool_name_re(tool: str) -> re.Pattern[str]:
    """Where a text names *tool*.  A name with ``_``, ``-`` or ``.`` in it is no word of prose: anywhere, as a whole
    name.  A one-word name (``terminal``, ``process``, ``patch``) is a tool only where the text marks it as code: in
    backticks, or called (``terminal(``); "the terminal state" names no tool."""
    escaped = re.escape(tool)
    if re.fullmatch(r"[A-Za-z0-9]+", tool):
        return re.compile(rf"`{escaped}`|(?<![\w.-]){escaped}\(")
    return re.compile(rf"(?<![\w.-]){escaped}(?![\w-])")


def tools_named(texts: Iterable[str | None], tools: Collection[str]) -> set[str]:
    """The *tools* the *texts* name (:func:`_tool_name_re`)."""
    joined = "\n".join(text for text in texts if text)
    if not joined:
        return set()
    return {tool for tool in tools if tool and tool in joined and _tool_name_re(tool).search(joined)}


def needs_unavailable_tool(skill: Any, unavailable: Collection[str]) -> bool:
    """Whether *skill* (a ``SkillDef`` or its dict) requires or names a tool in *unavailable*: one the session
    cannot be sent.  An expert is a model consulted, its text that model's prompt: never hidden by it."""
    if not unavailable:
        return False
    get = skill.get if isinstance(skill, dict) else (lambda key, default=None: getattr(skill, key, default))
    if get("type", "skill") == "expert":
        return False
    if set(get("requires_tools") or ()) & set(unavailable):
        return True
    return bool(tools_named((get("description"), get("trigger"), get("content")), unavailable))


def unavailable_tool_refusal(name: str) -> str:
    """What ``skill_view`` answers for a skill that needs a tool the session cannot be sent: no tool named."""
    return f"The {name} skill needs tools this session does not have. Do the work with the tools you have."


def fallback_refusal(name: str) -> str:
    """What ``skill_view`` answers for a fallback skill the session does not see."""
    return (
        f"{name} is for chats without the office tools: this chat makes and edits documents live with office_edit "
        "(the office skill)."
    )
