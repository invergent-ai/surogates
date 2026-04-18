"""Eager expansion of ``/<skill> args...`` user messages into skill content.

When a user message starts with ``/<name>`` (e.g. ``/arxiv cuda training``)
and ``<name>`` matches an available skill, this module:

1. Calls ``skill_view(name)`` server-side via the harness's tool registry,
   which fetches the skill body and (in production) auto-stages the skill's
   supporting files (``scripts/``, ``assets/``, ``templates/``,
   ``references/``) into the session sandbox bucket.
2. Rewrites the user message in-memory so the LLM sees the SKILL.md body
   inlined alongside the user's request, avoiding a round-trip and the
   chance the model picks a generic tool instead.

The original ``/<name> args...`` message remains in the event log untouched;
only the rebuilt-in-memory message handed to the LLM is rewritten.

The skill body returned by ``skill_view`` already includes a one-line
``staged_at`` preamble (added by the API route's ``_staging_preamble``)
when staging happened, so this module does not re-add staging guidance.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final

logger = logging.getLogger(__name__)

# ``/name`` (letters/digits/underscore/hyphen, must start with a letter),
# optionally followed by whitespace + arbitrary text.  ``re.DOTALL`` lets the
# trailing args span newlines (multi-line user messages).
_SLASH_COMMAND_RE: Final = re.compile(
    r"^/([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+(.*))?$",
    re.DOTALL,
)

# Slash commands handled elsewhere in the loop -- never treat these as skills.
_BUILTIN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset({"clear", "compress"})


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


async def expand_slash_skill(
    *,
    text: str,
    tools: Any,
    tenant: Any,
    session_id: str,
    api_client: Any | None,
    session_factory: Any | None,
) -> tuple[str, str, str | None] | None:
    """Try to expand a ``/<skill> args...`` user message.

    Returns ``(expanded_text, skill_name, staged_at)`` on success, or
    ``None`` when *text* is not a slash command, names a builtin, names an
    unknown skill, or the ``skill_view`` tool is unavailable.

    The function never raises -- failures degrade to ``None`` so the
    original user message reaches the LLM unchanged.
    """
    parsed = parse_slash_command(text)
    if parsed is None:
        return None
    name, args = parsed

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
    return expanded, name, staged_at
