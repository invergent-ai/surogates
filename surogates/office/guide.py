"""What the agent's first office call on each document type in a session carries: the office skill's rules, that
type's reference and its templates, so working by them never waits on the model choosing to open the skill.

Built-in skills live in the system bundle the API serves, not in the worker: each piece is fetched as the skill stores
it, through the skill's file route, which stages nothing into the workspace and adds nothing to the text. A type whose
rules or reference could not be fetched is not asked for again in this process for RETRY_AFTER_S. The templates are the
guide's optional part: a guide whose templates could not be fetched goes without them, and the skill still has them.
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
    """The office skill's rules (SKILL.md without its frontmatter), *kind*'s reference (references/<kind>.md) and its
    templates (templates/<kind>.md); None, logged, when the rules or the reference cannot be fetched, and for
    RETRY_AFTER_S after. The templates are left out for a type the skill has none for, whose file the API does not find,
    and, logged, when they cannot be fetched otherwise. Never raises: the call it goes with has already done its work."""
    if api_client is None or _unfetchable.get(kind, 0.0) > monotonic():
        return None
    skill, reference, templates = await asyncio.gather(
        _file(api_client, "SKILL.md"), _file(api_client, f"references/{kind}.md"), _file(api_client, f"templates/{kind}.md"),
        return_exceptions=True,
    )
    failed = next((fetched for fetched in (skill, reference) if isinstance(fetched, BaseException)), None)
    if failed is not None:
        _unfetchable[kind] = monotonic() + RETRY_AFTER_S
        if isinstance(failed, httpx.HTTPStatusError):
            # The API's answer (a bundle without the file, a skill this chat may not open) says it all.
            logger.warning("no office guide for %s for %.0f s: %s", kind, RETRY_AFTER_S, failed)
        else:
            logger.warning("no office guide for %s for %.0f s", kind, RETRY_AFTER_S, exc_info=failed)
        return None
    _unfetchable.pop(kind, None)
    guide = {"rules": _FRONTMATTER.sub("", skill, count=1).strip(), "reference": reference.strip()}
    if not isinstance(templates, BaseException):
        guide["templates"] = templates.strip()
    elif isinstance(templates, httpx.HTTPStatusError):
        # A 404 is a type the skill has no templates for; any other answer says it all.
        if templates.response.status_code != 404:
            logger.warning("the office guide for %s goes without its templates: %s", kind, templates)
    else:
        logger.warning("the office guide for %s goes without its templates", kind, exc_info=templates)
    return guide


async def _file(api_client: Any, path: str) -> str:
    content = (await api_client.skill_file_bytes(_SKILL, path)).decode("utf-8")
    if not content.strip():
        raise ValueError(f"the office skill's {path} is empty")
    return content
