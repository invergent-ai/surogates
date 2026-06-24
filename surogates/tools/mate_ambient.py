"""mate_ambient_post — the only path from an ambient turn to a channel post.

The handler is the hard gate: it resolves the channel from the SESSION config
(never a tool argument, so an ambient turn cannot post elsewhere), enforces the
rate caps + confidence threshold, and only then enqueues a Slack delivery.
"""

from __future__ import annotations

import json
from typing import Any

from surogates.ambient.decision import AmbientPostDecision, gate_decision
from surogates.ambient.rate_limiter import AmbientRateLimiter

MATE_AMBIENT_POST_SCHEMA: dict[str, Any] = {
    "name": "mate_ambient_post",
    "description": (
        "Post a proactive message into THIS channel during an ambient review. "
        "Use ONLY when you are confident the message is worth interrupting the "
        "channel for. Provide a 0-1 confidence; low-confidence or over-budget "
        "posts are suppressed. Do not use for normal replies."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to post."},
            "target_thread": {"type": "string", "description": "Slack thread_ts to reply in, or '' for the channel."},
            "confidence": {"type": "number", "description": "0-1 confidence this is worth posting."},
            "rationale": {"type": "string", "description": "Why this is worth posting."},
        },
        "required": ["message", "confidence"],
    },
}


async def handle_mate_ambient_post(
    args: dict[str, Any],
    *,
    agent_id: str,
    session_config: dict[str, Any],
    session_id: Any,
    redis: Any,
    delivery: Any,
    caps: dict[str, Any],
    **_ignored: Any,
) -> str:
    decision = AmbientPostDecision(
        action="post",
        target_thread=str(args.get("target_thread", "") or ""),
        confidence=float(args.get("confidence", 0.0)),
        message=str(args.get("message", "") or ""),
        rationale=str(args.get("rationale", "") or ""),
    )
    channel_id = session_config.get("slack_channel_id", "")
    if not channel_id:
        return json.dumps({"posted": False, "reason": "no channel bound to this session"})

    limiter = AmbientRateLimiter(redis)
    max_per_day = int(caps.get("max_proactive_posts_per_day", 10))
    min_gap = int(caps.get("min_seconds_between_posts", 600))
    threshold = float(caps.get("confidence_threshold", 0.7))

    limiter_ok = await limiter.allow_post(
        agent_id=agent_id, channel_id=channel_id,
        max_per_day=max_per_day, min_seconds_between=min_gap,
    )
    if decision.target_thread and limiter_ok:
        window = int(caps.get("quiet_thread_minutes", 120)) * 60
        if not await limiter.allow_revive(
            agent_id=agent_id, thread_ts=decision.target_thread, window_seconds=window,
        ):
            limiter_ok = False

    if not gate_decision(decision, confidence_threshold=threshold, limiter_ok=limiter_ok):
        return json.dumps({"posted": False, "reason": "suppressed by gate/caps"})

    # Unique per-post sequence so DeliveryService's dedupe_key (channel:event_id)
    # never collides across multiple posts in one ambient turn.
    seq = await redis.incr(f"mate:ambient:postseq:{agent_id}:{channel_id}")
    await delivery.enqueue(
        session_id=session_id,
        event_id=int(seq),
        channel="slack",
        destination={
            "channel_id": channel_id,
            "thread_ts": decision.target_thread or None,
            "team_id": session_config.get("slack_team_id", ""),
        },
        payload={"content": decision.message},
    )
    await limiter.record_post(agent_id=agent_id, channel_id=channel_id)
    await limiter.record_post_gap(
        agent_id=agent_id, channel_id=channel_id, min_seconds_between=min_gap,
    )
    return json.dumps({"posted": True, "reason": "delivered"})


async def _mate_ambient_post_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    """Registry adapter: gate on ambient sessions, build delivery, read caps.

    The pure :func:`handle_mate_ambient_post` is caps-agnostic; this adapter
    threads the harness dispatch context into it.  Only ambient sessions (which
    carry ``ambient: True`` + ``ambient_caps`` in config) may post.
    """
    session_config = kwargs.get("session_config") or {}
    if not session_config.get("ambient"):
        return json.dumps({"posted": False, "reason": "not an ambient session"})

    from surogates.channels.delivery import DeliveryService

    delivery = DeliveryService(kwargs["session_factory"], kwargs["redis"])
    return await handle_mate_ambient_post(
        arguments,
        agent_id=kwargs.get("agent_id", ""),
        session_config=session_config,
        session_id=kwargs.get("session_id"),
        redis=kwargs["redis"],
        delivery=delivery,
        caps=session_config.get("ambient_caps") or {},
    )


def register(registry: Any) -> None:
    """Register mate_ambient_post on the tool registry."""
    from surogates.tools.registry import ToolSchema

    registry.register(
        name="mate_ambient_post",
        schema=ToolSchema(
            name=MATE_AMBIENT_POST_SCHEMA["name"],
            description=MATE_AMBIENT_POST_SCHEMA["description"],
            parameters=MATE_AMBIENT_POST_SCHEMA["parameters"],
        ),
        handler=_mate_ambient_post_handler,
        toolset="mate",
    )
