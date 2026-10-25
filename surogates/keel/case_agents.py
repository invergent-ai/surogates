"""A person's case agent: one person's agent on one case, which reads the case only through Keel.

Keel starts its session through the internal link, which stamps which case
and which person into the config of the agent's own session under
``case_agent`` (:data:`~surogates.channels.memory_boundary.CASE_AGENT_KEY`),
beside the case's ``case:`` memory boundary.  The session is owned by the
org's :data:`CASE_AGENT_ACCOUNT` service account, never by the person: Keel
acts as the person when the agent reads the case.  Its boundary is
memory-only: it works in a scratch workspace of its own, never the case's.
No client sets the key: every other create path strips it, and a child
never copies it.

The readers tolerate partial objects, as the step's do: a missing attribute
or a config that is not a mapping reads as no case agent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from surogates.channels.memory_boundary import CASE_AGENT_KEY, CASE_BOUNDARY_PREFIX
from surogates.keel.steps import _read

__all__ = [
    "CASE_AGENT_ACCOUNT",
    "CaseAgentRef",
    "case_agent_in",
    "case_agent_of",
    "is_case_agent",
]

#: The name of the org's service account that owns every case agent's session.
CASE_AGENT_ACCOUNT = "keel-case-agent"


class CaseAgentRef(BaseModel):
    """Which case a case agent works on, and for which person.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    case_id: UUID
    case_number: str
    # The user of the org whose agent this is.
    person_id: UUID


def case_agent_in(config: Mapping[str, Any] | None, *, session_id: object = None) -> CaseAgentRef | None:
    """The case agent's reference a session's config holds, or ``None``; an invalid one is logged and ignored."""
    return _read(CaseAgentRef, config, CASE_AGENT_KEY, session_id)


def case_agent_of(session: object) -> CaseAgentRef | None:
    """The case agent's reference of *session*, or ``None``."""
    return case_agent_in(getattr(session, "config", None), session_id=getattr(session, "id", None))


def is_case_agent(session: object) -> bool:
    """True for a case agent's own session: a root session holding a valid
    reference, inside the memory boundary of the case it names."""
    if getattr(session, "parent_id", None) is not None:
        return False
    ref = case_agent_of(session)
    if ref is None:
        return False
    config = getattr(session, "config", None) or {}
    return str(config.get("memory_boundary") or "").strip() == f"{CASE_BOUNDARY_PREFIX}{ref.case_id}"
