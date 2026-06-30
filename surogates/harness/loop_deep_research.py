"""Deep-research planner helpers for the harness loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from surogates.session.models import Session


DEEP_RESEARCH_NO_DELEGATE_NUDGE: str = (
    "You stopped without emitting a `delegate_task` tool call.  Your "
    "outline and evidence bank are ready, but the writer was never "
    "spawned, so the user has no report.\n\n"
    "Prose like \"Now handing off to the writer\" or a `<next_action>` "
    "block DOES NOT trigger the delegation -- only an actual "
    "`delegate_task` tool call does.\n\n"
    "Emit `delegate_task(agent_type=\"research-writer\", goal=<full "
    "outline>, context=<original question + bank reminder>)` as your "
    "next tool call.  If you legitimately cannot hand off (e.g. the "
    "bank is empty), say so plainly and stop -- do not narrate a "
    "handoff that isn't happening."
)
# The orphan-completion nudge above intentionally still mentions a
# ``<next_action>`` block: the model occasionally narrates one even
# though the harness no longer instructs or parses it, and the nudge
# reminds the model that prose -- of any shape -- does not trigger a
# delegation.


def _is_deep_research_planner(session: Session) -> bool:
    """True iff the session is running as the deep-research planner."""
    if session is None:
        return False
    config = session.config or {}
    return config.get("agent_type") == "deep-research"


def _planner_already_delegated_to_writer(
    messages: list[dict[str, Any]] | None,
) -> bool:
    """Return True when the in-memory message list shows a prior
    ``delegate_task(agent_type="research-writer")`` call on this turn
    so the orphan-completion nudge does not fire on a planner that
    already did its job.

    Reads ``messages`` (not the wake's ``all_events`` snapshot)
    because ``all_events`` is captured ONCE at wake start and does
    not include tool calls emitted during the current wake.  A
    planner that just finished a successful delegate_task earlier
    this same wake would otherwise look like one that never
    delegated -- triggering the nudge and a duplicate writer spawn.
    """
    if not messages:
        return False
    import json as _json
    for message in messages:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            if fn.get("name") != "delegate_task":
                continue
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = _json.loads(raw_args) if raw_args else {}
                except _json.JSONDecodeError:
                    continue
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                continue
            # ``delegate_task`` carries either a scalar ``agent_type`` or
            # a ``goals`` array whose items each have their own
            # ``agent_type``.  Honor both shapes.
            if args.get("agent_type") == "research-writer":
                return True
            for item in args.get("goals") or []:
                if (
                    isinstance(item, dict)
                    and item.get("agent_type") == "research-writer"
                ):
                    return True
    return False
