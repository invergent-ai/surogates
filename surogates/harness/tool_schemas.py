"""Per-session tool schema post-processing.

Strips parameters that cannot resolve for the current tenant so the
LLM never sees them and cannot hallucinate values.  Currently gates the
``agent_type`` parameter on ``delegate_task`` / ``spawn_worker``: when
the tenant has no enabled sub-agents, no value for it can resolve, so
it is removed from the exported schema.
"""

from __future__ import annotations

import copy
from typing import Any

_AGENT_TYPE_GATED_TOOLS: frozenset[str] = frozenset({
    "delegate_task",
    "spawn_worker",
})


def filter_schemas_for_tenant(
    schemas: list[dict[str, Any]],
    *,
    has_agents: bool,
) -> list[dict[str, Any]]:
    """Return *schemas* with tenant-conditional parameters stripped.

    When *has_agents* is ``False``, the ``agent_type`` property is
    removed from :data:`_AGENT_TYPE_GATED_TOOLS`.  Input is never
    mutated -- affected entries are deep-copied, untouched entries are
    returned by reference.  When *has_agents* is ``True`` the input
    list is returned unchanged.
    """
    if has_agents:
        return schemas

    filtered: list[dict[str, Any]] = []
    for schema in schemas:
        name = schema["function"]["name"]
        if name not in _AGENT_TYPE_GATED_TOOLS:
            filtered.append(schema)
            continue

        clone = copy.deepcopy(schema)
        clone["function"]["parameters"]["properties"].pop("agent_type", None)
        filtered.append(clone)

    return filtered
