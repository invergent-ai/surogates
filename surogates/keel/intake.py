"""The case types an intake agent may start, as Keel's door lists them.

An intake agent is one that case types name as their front door: it talks
with people, and starts their cases with ``start_case``.  The worker asks at
each wake, and offers ``start_case`` only when the list is not empty.  Each
list is kept :data:`INTAKE_CACHE_SECONDS` per project and agent, an empty
one too: a runtime with no link to Keel, or a Keel that could not be
reached, offers no ``start_case`` until the next ask.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from surogates.keel.client import keel_client

__all__ = ["INTAKE_CACHE_SECONDS", "intake_case_types"]

logger = logging.getLogger(__name__)

#: How long one list of case types is kept, per project and agent.
INTAKE_CACHE_SECONDS = 60
#: How many agents' lists a worker keeps, the oldest dropped first.
_KEPT = 4096

#: Each (project, agent)'s list, and when it is to be asked again.
_cache: OrderedDict[tuple[str, str], tuple[float, list[dict]]] = OrderedDict()
_now = time.monotonic


def _granted(listed: list[Any]) -> list[dict]:
    """Keel's list as the tool reads it: each type once, with a name and the names of its required fields."""
    types: list[dict] = []
    seen: set[str] = set()
    for entry in listed:
        if not isinstance(entry, dict):
            continue
        key = entry.get("case_type")
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        name = entry.get("name")
        form = entry.get("form")
        required = entry.get("required")
        types.append({
            "case_type": key,
            "name": name if isinstance(name, str) and name else key,
            "form": form if isinstance(form, dict) else {},
            "required": [field for field in required if isinstance(field, str)] if isinstance(required, list) else [],
        })
    return types


async def _ask(org_id: object, agent_id: str) -> list[dict]:
    """Keel's list for *agent_id* in project *org_id*; ``[]`` when there is none to have.  Never raises."""
    client = keel_client()
    if client is None:
        return []
    try:
        answer = await client.intake(org_id, agent_id)
    except Exception as exc:
        logger.warning("Keel was not asked which case types agent %s may start (%s)", agent_id, type(exc).__name__)
        return []
    if answer.status != 200 or not isinstance(answer.data, list):
        if answer.status is not None:
            logger.warning(
                "Keel did not list the case types agent %s may start (%s %s)",
                agent_id, answer.status, answer.code or "",
            )
        return []
    return _granted(answer.data)


async def intake_case_types(org_id: object, agent_id: str) -> list[dict]:
    """The case types *agent_id* may start in project *org_id*: each ``{"case_type", "name", "form", "required"}``.

    Asked of Keel at most once a :data:`INTAKE_CACHE_SECONDS` per project and
    agent.  ``[]`` when the agent may start none, when this runtime has no
    link to Keel, and when Keel could not be reached.  Never raises.
    """
    key = (str(org_id), str(agent_id))
    now = _now()
    held = _cache.get(key)
    if held is not None and now < held[0]:
        return list(held[1])
    types = await _ask(org_id, agent_id)
    _cache[key] = (now + INTAKE_CACHE_SECONDS, types)
    _cache.move_to_end(key)
    while len(_cache) > _KEPT:
        _cache.popitem(last=False)
    return list(types)
