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
    "spawn_task",
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


# Tools whose backing resource is decidable from the agent's config. Shipping
# them when the resource is absent is pure overhead: the model cannot use a KB
# that is not attached or post to a channel that does not exist. Measured on
# the GAIA agent, 21 of 45 shipped tools were never called across 270
# sessions, costing ~5,825 tokens on every request.
_KB_TOOLS: frozenset[str] = frozenset({
    "kb_search_pages", "kb_list_pages", "kb_read_page",
})
_CHANNEL_TOOLS: frozenset[str] = frozenset({
    "fetch_channel_messages", "fetch_channel_file", "mate_ambient_post",
})
_CRON_TOOLS: frozenset[str] = frozenset({
    "cron_create", "cron_list", "cron_delete",
})
_WHITEBOARD_TOOLS: frozenset[str] = frozenset({
    "whiteboard_draw",
})


def drop_unusable_tools(
    schemas: list[dict[str, Any]],
    *,
    has_kbs: bool,
    has_channel: bool,
    is_scheduled: bool,
    is_whiteboard: bool = False,
) -> list[dict[str, Any]]:
    """Drop tools whose backing resource this agent does not have.

    Deliberately conservative: only gates on facts already known from the
    runtime config, never on usage history. A tool that is merely unused
    stays, because "not called yet" is not "cannot be called" -- that
    distinction is what makes this safe to apply to every agent rather
    than to one benchmark workload.

    Never returns an empty list: a request with no tools at all is worse
    than an oversized one.
    """
    drop: set[str] = set()
    if not has_kbs:
        drop |= _KB_TOOLS
    if not has_channel:
        drop |= _CHANNEL_TOOLS
    if not is_scheduled:
        drop |= _CRON_TOOLS
    if not is_whiteboard:
        drop |= _WHITEBOARD_TOOLS
    if not drop:
        return schemas

    kept = [s for s in schemas if s["function"]["name"] not in drop]
    return kept or schemas
