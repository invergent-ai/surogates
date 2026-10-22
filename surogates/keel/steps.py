"""A case's step sessions: the contract Keel starts a step with, and the case's scope.

Keel starts an agent's step session through the internal link, and the link
stamps two server-owned keys into its config:

- ``case_step``, the step contract, on the step's own session only;
- ``case_scope``, the tool and knowledge narrowing, which every child copies
  from its parent.

Every session of the case, children included, also carries the case's
``case:`` memory boundary.  No client sets any of them: every other create
path strips them.

Each reader has two forms.  The ``*_of`` / ``is_*_session`` form takes a
session; the ``*_in`` / ``is_case_config`` form takes only its config, for
tools, which hold just that.  Both tolerate partial objects: a missing
attribute or a config that is not a mapping reads as no case.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from surogates.channels.memory_boundary import (
    CASE_BOUNDARY_PREFIX,
    is_case_config,
    is_case_session,
)

__all__ = [
    "CASE_BOUNDARY_PREFIX",
    "CASE_SCOPE_KEY",
    "CASE_STEP_KEY",
    "CaseScope",
    "OutcomeContract",
    "StepContract",
    "case_scope_in",
    "case_scope_of",
    "is_case_config",
    "is_case_session",
    "is_case_step",
    "step_contract_in",
    "step_contract_of",
]

logger = logging.getLogger(__name__)

_M = TypeVar("_M", bound=BaseModel)

#: The step contract, in the config of the step's own session.
CASE_STEP_KEY = "case_step"
#: The case's tool and knowledge narrowing, in the config of every session of the case.
CASE_SCOPE_KEY = "case_scope"


class OutcomeContract(BaseModel):
    """One outcome the step may end with, and the outputs it requires."""

    model_config = ConfigDict(extra="ignore")

    required: list[str] = Field(default_factory=list)


class StepContract(BaseModel):
    """What Keel asks of one agent step.  Keys this runtime does not know are ignored."""

    model_config = ConfigDict(extra="ignore")

    case_id: UUID
    case_number: str
    case_type: str
    type_version: int
    token: str
    node: str
    step: str
    # ``draft``: the agent proposes, and a person reviews and submits.
    mode: Literal["complete", "draft"]
    # The inputs the case chose.  Emptied once the step ends for the session.
    inputs: dict[str, Any] = Field(default_factory=dict)
    # The step form, as JSON Schema.
    form: dict[str, Any]
    # Each file field takes one file, or a list of them.
    files: dict[str, Literal["one", "list"]] = Field(default_factory=dict)
    outcomes: dict[str, OutcomeContract]
    # The messages of the step's business checks.
    checks: list[str] = Field(default_factory=list)
    # ``None`` is the agent's own; a list narrows.  ``tools`` narrows work tools only.
    tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    # The full knowledge grant, possibly empty.
    knowledge: list[str] = Field(default_factory=list)


class CaseScope(BaseModel):
    """What every session of a case may use: its work tools and the knowledge granted it."""

    model_config = ConfigDict(extra="ignore")

    # ``None`` keeps the agent's own work tools.
    tools: list[str] | None = None
    knowledge: list[str] = Field(default_factory=list)


def _read(model: type[_M], config: object, key: str, session_id: object) -> _M | None:
    raw = config.get(key) if isinstance(config, Mapping) else None
    if raw is None:
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        # The session id only: the error would quote the case's data.
        logger.warning("Ignoring an invalid %s in session %s", key, session_id)
        return None


def step_contract_in(config: Mapping[str, Any] | None, *, session_id: object = None) -> StepContract | None:
    """The step contract a session's config holds, or ``None``; an invalid one is logged and ignored."""
    return _read(StepContract, config, CASE_STEP_KEY, session_id)


def case_scope_in(config: Mapping[str, Any] | None, *, session_id: object = None) -> CaseScope | None:
    """The case scope a session's config holds, or ``None``; an invalid one is logged and ignored."""
    return _read(CaseScope, config, CASE_SCOPE_KEY, session_id)


def step_contract_of(session: object) -> StepContract | None:
    """The step contract of *session*, or ``None``."""
    return step_contract_in(getattr(session, "config", None), session_id=getattr(session, "id", None))


def case_scope_of(session: object) -> CaseScope | None:
    """The case scope of *session*, or ``None``."""
    return case_scope_in(getattr(session, "config", None), session_id=getattr(session, "id", None))


def is_case_step(session: object) -> bool:
    """True for the step's own session: a root session holding a valid contract.

    A child of the step works on the case too, but is not the step.
    """
    return getattr(session, "parent_id", None) is None and step_contract_of(session) is not None
