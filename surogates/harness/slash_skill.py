"""Eager expansion of ``/<skill> args...`` and ``/<expert> args...`` user messages.

Two paths converge here. Regular skills inline their SKILL.md body
(staging supporting files if present). Active experts spawn a mini-loop
via :class:`ExpertConsultationService`, and the deliverable is inlined
into the user message so the base LLM reviews and relays.

The original ``/<name> args...`` message remains in the event log
untouched; only the rebuilt-in-memory message handed to the LLM is
rewritten.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final, Literal
from uuid import UUID

logger = logging.getLogger(__name__)

# ``/name`` (letters/digits/underscore/hyphen, must start with a letter),
# optionally followed by whitespace + arbitrary text.  ``re.DOTALL`` lets
# the trailing args span newlines (multi-line user messages).
_SLASH_COMMAND_RE: Final = re.compile(
    r"^/([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+(.*))?$",
    re.DOTALL,
)

# Slash commands handled elsewhere in the loop -- never treat these as skills.
_BUILTIN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset({
    "clear",
    "compress",
    "goal",
    "loop",
    "mission",
})


def parse_slash_command(text: str) -> tuple[str, str] | None:
    """Return ``(name, args)`` if *text* is a slash command, else ``None``.

    Builtin commands (``/clear``, ``/compress``) return ``None`` so they
    flow through to their dedicated handlers in the harness loop.
    """
    match = _SLASH_COMMAND_RE.match(text.strip())
    if match is None:
        return None
    name = match.group(1)
    if name in _BUILTIN_SLASH_COMMANDS:
        return None
    args = (match.group(2) or "").strip()
    return name, args


def build_expanded_message(*, name: str, args: str, skill_body: str) -> str:
    """Build the rewritten user message with the skill body inlined.

    The ``skill_body`` should be the ``content`` field returned by
    ``skill_view``; in production it already starts with a staged-path
    preamble so relative paths (``scripts/foo.py``) resolve correctly.
    """
    lines: list[str] = [
        f"The user invoked the `{name}` skill"
        + (f" with: {args}" if args else "")
        + ".",
        "",
        "Use the following skill to handle this request:",
        "",
        "---",
        skill_body,
        "---",
    ]
    if args:
        lines.extend(["", f"User request: {args}"])
    return "\n".join(lines)


def build_expert_expanded_message(
    *, name: str, args: str, deliverable: str,
) -> str:
    """Build the rewritten user message with the expert deliverable inlined.

    The deliverable is presented as the expert's reply; the base LLM
    reviews and relays in the same turn.
    """
    return (
        f"[Expert {name} delivered:]\n"
        f"{deliverable}\n\n"
        f"User request: {args}"
    )


async def _load_skills_for_slash(tenant: Any, **kwargs: Any) -> list:
    """Load the tenant skill catalog for slash-command resolution.

    Wraps ``surogates.tools.builtin.skills._load_all_skills`` so tests
    can monkey-patch this single seam without going through the
    underlying dispatch.
    """
    from surogates.tools.builtin.skills import _load_all_skills

    return await _load_all_skills(tenant, **kwargs)


async def expand_slash_skill(
    *,
    text: str,
    tools: Any,
    tenant: Any,
    session_id: str,
    api_client: Any | None,
    session_factory: Any | None,
    session_store: Any | None = None,
    sandbox_pool: Any | None = None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
    """Try to expand a ``/<name> args...`` user message.

    Returns ``(expanded_text, name, staged_at, kind)`` on success, or
    ``None`` when *text* is not a slash command, names a builtin, names
    an unknown skill/expert, or expansion failed.  ``kind`` is
    ``"expert"`` when an active expert handled the invocation,
    otherwise ``"skill"``.

    The function never raises -- failures degrade to ``None`` so the
    original user message reaches the LLM unchanged.
    """
    parsed = parse_slash_command(text)
    if parsed is None:
        return None
    name, args = parsed

    # Look up the named entry in the tenant catalog so we can branch
    # on type before dispatching skill_view.  An active expert routes to
    # the mini-loop; everything else (regular skill, draft/retired expert,
    # unknown name) falls through to the legacy skill_view path.
    try:
        catalog = await _load_skills_for_slash(
            tenant,
            api_client=api_client,
            session_factory=session_factory,
        )
    except Exception:
        logger.debug(
            "Slash catalog load failed for /%s; falling back to skill path",
            name,
            exc_info=True,
        )
        catalog = []

    matched = next((s for s in catalog if s.name == name), None)
    if matched is not None and getattr(matched, "is_active_expert", False):
        return await _expand_expert(
            expert=matched,
            args=args,
            tenant=tenant,
            session_id=session_id,
            tool_registry=tools,
            session_store=session_store,
            sandbox_pool=sandbox_pool,
        )

    return await _expand_skill(
        name=name,
        args=args,
        tools=tools,
        tenant=tenant,
        session_id=session_id,
        api_client=api_client,
        session_factory=session_factory,
    )


async def _expand_skill(
    *,
    name: str,
    args: str,
    tools: Any,
    tenant: Any,
    session_id: str,
    api_client: Any | None,
    session_factory: Any | None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
    """Inline a regular skill's body via ``skill_view``.

    Returns ``None`` when the skill is unknown or staging failed so the
    caller falls through to the verbatim user message.
    """
    try:
        result = await tools.dispatch(
            "skill_view",
            {"name": name},
            tenant=tenant,
            session_id=session_id,
            api_client=api_client,
            session_factory=session_factory,
        )
    except Exception:
        logger.debug(
            "skill_view dispatch failed for /%s; passing through verbatim",
            name,
            exc_info=True,
        )
        return None

    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        logger.debug("skill_view returned non-JSON for /%s", name)
        return None

    if not payload.get("success"):
        # Unknown skill or staging error -- let the LLM see the raw /name
        # so it can prompt the user or use skills_list itself.
        return None

    skill_body = payload.get("content") or ""
    if not skill_body:
        return None
    staged_at = payload.get("staged_at")

    expanded = build_expanded_message(name=name, args=args, skill_body=skill_body)
    return expanded, name, staged_at, "skill"


async def _expand_expert(
    *,
    expert: Any,
    args: str,
    tenant: Any,
    session_id: str,
    tool_registry: Any,
    session_store: Any | None,
    sandbox_pool: Any | None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
    """Run the expert mini-loop and inline the deliverable.

    Returns ``None`` when the user supplied no task body so the caller
    falls through to the verbatim user message (giving the LLM or user a
    chance to clarify).  Errors during consultation are returned as an
    expert-shaped expanded message so the base LLM can surface the
    failure to the user instead of silently dropping the request.
    """
    if not args:
        return None

    try:
        from surogates.tools.builtin.expert_service import ExpertConsultationService

        service = ExpertConsultationService(
            tenant=tenant,
            session_id=UUID(session_id),
            tool_registry=tool_registry,
            session_store=session_store,
            sandbox_pool=sandbox_pool,
        )
        outcome = await service.consult(expert=expert, task=args)
    except Exception:
        logger.exception(
            "Expert consultation failed for /%s; passing through verbatim",
            expert.name,
        )
        return None

    deliverable = outcome.content if outcome.success else (outcome.content or "")
    expanded = build_expert_expanded_message(
        name=expert.name, args=args, deliverable=deliverable,
    )
    return expanded, expert.name, None, "expert"
