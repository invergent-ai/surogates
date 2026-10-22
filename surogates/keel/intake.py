"""The case types an intake agent may start, as Keel's door lists them.

An intake agent is one that case types name as their front door: it talks
with people, and starts their cases with ``start_case``.  The worker asks at
each wake, and offers ``start_case`` only when the list is not empty.  Each
list is kept :data:`INTAKE_CACHE_SECONDS` per project and agent, an empty
one too: a Keel that could not be reached offers no ``start_case`` until
the next ask.  A runtime with no link to Keel asks nothing, and keeps
nothing.

The ask is on the wake's path, so it is bounded: a Keel that has not
answered in :data:`INTAKE_ASK_SECONDS` grants nothing until the next ask.
The wakes that need one agent's list while it is being asked wait on that
one ask, and never ask again themselves.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import OrderedDict
from typing import Any

from surogates.keel.client import KeelClient, keel_client

__all__ = ["INTAKE_CACHE_SECONDS", "intake_case_types"]

logger = logging.getLogger(__name__)

#: How long one list of case types is kept, per project and agent.
INTAKE_CACHE_SECONDS = 60
#: The longest one ask may hold a wake, in seconds.
INTAKE_ASK_SECONDS = 2.0
#: How many agents' lists a worker keeps, the oldest dropped first.
_KEPT = 4096

#: Each (project, agent)'s list, and when it is to be asked again.
_cache: OrderedDict[tuple[str, str], tuple[float, list[dict]]] = OrderedDict()
#: The ask under way for each (project, agent), which every wake needing its list waits on.
_asking: dict[tuple[str, str], asyncio.Future[list[dict]]] = {}
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


async def _ask(client: KeelClient, org_id: object, agent_id: str) -> list[dict]:
    """Keel's list for *agent_id* in project *org_id*; ``[]`` when there is none to have.  Never raises."""
    try:
        answer = await asyncio.wait_for(client.intake(org_id, agent_id), INTAKE_ASK_SECONDS)
    except TimeoutError:
        logger.warning(
            "Keel did not say within %s s which case types agent %s may start", INTAKE_ASK_SECONDS, agent_id,
        )
        return []
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


async def _refresh(client: KeelClient, key: tuple[str, str], org_id: object, agent_id: str) -> list[dict]:
    """Ask Keel for *key*'s list, and keep it for the minute."""
    types = await _ask(client, org_id, agent_id)
    _cache[key] = (_now() + INTAKE_CACHE_SECONDS, types)
    _cache.move_to_end(key)
    while len(_cache) > _KEPT:
        _cache.popitem(last=False)
    return types


def _ended(key: tuple[str, str], ask: asyncio.Future[list[dict]]) -> None:
    """*key*'s ask is no longer under way, however it ended: cancelled before it ran, too."""
    if _asking.get(key) is ask:
        del _asking[key]


async def intake_case_types(org_id: object, agent_id: str) -> list[dict]:
    """The case types *agent_id* may start in project *org_id*: each ``{"case_type", "name", "form", "required"}``.

    Asked of Keel at most once a :data:`INTAKE_CACHE_SECONDS` per project and
    agent, by one ask that every wake needing the list meanwhile waits on,
    and that waits at most :data:`INTAKE_ASK_SECONDS`.  ``[]`` when the agent
    may start none, when this runtime has no link to Keel, and when Keel
    could not be reached or did not answer in time.  Never raises: a wake
    whose ask was cancelled under it, not the wake itself, also gets ``[]``.
    """
    client = keel_client()
    if client is None:
        return []
    key = (str(org_id), str(agent_id))
    held = _cache.get(key)
    if held is not None and _now() < held[0]:
        return list(held[1])
    asking = _asking.get(key)
    if asking is None:
        asking = _asking[key] = asyncio.ensure_future(_refresh(client, key, org_id, agent_id))
        asking.add_done_callback(functools.partial(_ended, key))
    # Shielded: a wake cancelled while it waits leaves the ask to the others.
    try:
        return list(await asyncio.shield(asking))
    except asyncio.CancelledError:
        wake = asyncio.current_task()
        if asking.cancelled() and wake is not None and not wake.cancelling():
            return []
        raise
