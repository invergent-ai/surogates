"""What the agent's first office call on each document type in a session carries: the office skill's rules and that
type's reference, so working by them never waits on the model choosing to open the skill.

Built-in skills live in the system bundle the API serves, not in the worker: each piece is fetched as the skill stores
it, through the skill's file route, which stages nothing into the workspace and adds nothing to the text. A type whose
guide could not be fetched is not asked for again in this process for RETRY_AFTER_S.
"""

from __future__ import annotations

import asyncio
import logging
import re
from time import monotonic
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: The session config's list of the document types whose guide the agent was given.
GUIDES_KEY = "office_guides"
#: How long a document type whose guide could not be fetched goes without it before it is asked for again, in seconds.
RETRY_AFTER_S = 300.0
_SKILL = "office"
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# The document types whose guide could not be fetched, and until when (monotonic()) they go without it.
_unfetchable: dict[str, float] = {}


async def guide_for(kind: str, api_client: Any) -> dict[str, str] | None:
    """The office skill's rules (SKILL.md without its frontmatter) and *kind*'s reference (references/<kind>.md);
    None, logged, when either cannot be fetched, and for RETRY_AFTER_S after. Never raises: the call it goes with has
    already done its work."""
    if api_client is None or _unfetchable.get(kind, 0.0) > monotonic():
        return None
    try:
        skill, reference = await asyncio.gather(_file(api_client, "SKILL.md"), _file(api_client, f"references/{kind}.md"))
    except Exception as exc:
        _unfetchable[kind] = monotonic() + RETRY_AFTER_S
        if isinstance(exc, httpx.HTTPStatusError):
            # The API's answer (a bundle without the file, a skill this chat may not open) says it all.
            logger.warning("no office guide for %s for %.0f s: %s", kind, RETRY_AFTER_S, exc)
        else:
            logger.warning("no office guide for %s for %.0f s", kind, RETRY_AFTER_S, exc_info=True)
        return None
    _unfetchable.pop(kind, None)
    return {"rules": _FRONTMATTER.sub("", skill, count=1).strip(), "reference": reference.strip()}


async def _file(api_client: Any, path: str) -> str:
    content = (await api_client.skill_file_bytes(_SKILL, path)).decode("utf-8")
    if not content.strip():
        raise ValueError(f"the office skill's {path} is empty")
    return content
