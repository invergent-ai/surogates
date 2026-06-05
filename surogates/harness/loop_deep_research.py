"""Deep-research planner helpers for the harness loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from surogates.harness.expert_routing import parse_next_action_complexity

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


def _prior_next_action_complexity(
    messages: list[dict[str, Any]],
) -> str | None:
    """Return the complexity declared by the latest assistant turn, or ``None``.

    Reads the most recent assistant message's full text (not the
    truncated excerpt — the ``<next_action>`` footer can land anywhere
    in a long answer) and parses the ``<next_action complexity="...">``
    block via :func:`parse_next_action_complexity`.

    Returns ``None`` when there is no prior assistant turn (turn 1 of
    a session) OR when the model failed to emit the directive (older
    sessions, prompt drift).  Callers treat ``None`` as "no signal —
    fall through to the classifier" and ``low``/``medium``/``high`` as
    the model's self-reported intent.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            text = " ".join(
                str(part.get("text", ""))
                for part in content
                if (
                    isinstance(part, dict)
                    and part.get("type") in {"text", "output_text"}
                )
            )
        else:
            text = str(content)
        return parse_next_action_complexity(text)
    return None
