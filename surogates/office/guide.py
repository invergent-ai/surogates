"""What the agent's first office call on each document type in a session carries: the office skill's rules and that
type's reference, so working by them never waits on the model choosing to open the skill.

Built-in skills live in the system bundle the API serves, not in the worker: each piece is fetched through the skill's
file route, which stages nothing into the workspace and adds nothing to the text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: The session config's list of the document types whose guide the agent was given.
GUIDES_KEY = "office_guides"
_SKILL = "office"
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


async def guide_for(kind: str, api_client: Any) -> dict[str, str] | None:
    """The office skill's rules (SKILL.md without its frontmatter) and *kind*'s reference (references/<kind>.md);
    None, logged, when either cannot be fetched. Never raises: the call it goes with has already done its work."""
    if api_client is None:
        return None
    try:
        skill, reference = await asyncio.gather(_file(api_client, "SKILL.md"), _file(api_client, f"references/{kind}.md"))
    except Exception:
        logger.warning("could not fetch the office skill's rules and %s reference", kind, exc_info=True)
        return None
    return {"rules": _FRONTMATTER.sub("", skill, count=1).strip(), "reference": reference.strip()}


async def _file(api_client: Any, path: str) -> str:
    answer = json.loads(await api_client.view_skill(_SKILL, path))
    content = answer.get("content")
    if not answer.get("success") or answer.get("binary") or not isinstance(content, str) or not content.strip():
        raise ValueError(f"the office skill's {path} came back without its text: {answer.get('error') or 'empty'}")
    return content
