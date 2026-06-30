"""Materialize one ambient review tick into the dedicated ambient session."""

from __future__ import annotations

from uuid import UUID, uuid4

from surogates.ambient.prompt import build_ambient_prompt
from surogates.ambient.store import AmbientSchedule
from surogates.ambient.tasks_probe import recent_task_changes
from surogates.config import enqueue_session
from surogates.session.store import SessionNotFoundError


async def materialize_ambient_tick(
    schedule: AmbientSchedule,
    *,
    session_store,
    ambient_store,
    session_factory,
    settings,
    redis,
) -> UUID:
    """Ensure the dedicated ambient session, inject the prompt, enqueue, advance.

    The ambient session is ``channel="ambient"`` so it never auto-delivers to
    Slack; it shares the channel memory bank via ``slack_channel_id`` and posts
    only through the gated ``mate_ambient_post`` tool.  Returns the ambient
    session id.
    """
    ambient_session_id = schedule.ambient_session_id
    if ambient_session_id is None:
        # Inherit the source channel session's principal so the ambient
        # session discovers the SAME toolset as a normal turn (internal +
        # MCP + Composio, treated uniformly).  Falls back to userless when the
        # source session is gone (then only internal builtins are available).
        principal_user_id = None
        principal_sa_id = None
        if schedule.source_session_id is not None:
            try:
                src = await session_store.get_session(schedule.source_session_id)
                principal_user_id = src.user_id
                principal_sa_id = src.service_account_id
            except SessionNotFoundError:
                pass
        ambient_session_id = uuid4()
        sched_config = schedule.config or {}
        await session_store.create_session(
            session_id=ambient_session_id,
            user_id=principal_user_id,
            service_account_id=principal_sa_id,
            org_id=schedule.org_id,
            agent_id=schedule.agent_id,
            channel="ambient",
            model=settings.llm.model,
            config={
                "slack_channel_id": schedule.channel_id,
                "slack_team_id": sched_config.get("slack_team_id", ""),
                "ambient": True,
                "ambient_caps": sched_config.get("ambient_caps", {}),
                "ambient_source_session_id": str(schedule.source_session_id)
                if schedule.source_session_id else "",
            },
        )

    task_changes: list[str] = []
    if schedule.source_session_id is not None and session_factory is not None:
        task_changes = await recent_task_changes(
            session_factory,
            org_id=schedule.org_id,
            source_session_id=schedule.source_session_id,
        )

    prompt = build_ambient_prompt(
        channel_label=f"#{schedule.channel_id}",
        task_changes=task_changes,
    )
    await session_store.emit_synthetic_user_message(
        ambient_session_id,
        content=prompt,
        synthetic="ambient_tick",
        metadata={"ambient_schedule_id": str(schedule.id)},
    )
    await enqueue_session(
        redis,
        org_id=str(schedule.org_id),
        agent_id=schedule.agent_id,
        session_id=ambient_session_id,
    )
    await ambient_store.mark_fired(schedule, ambient_session_id=ambient_session_id)
    return ambient_session_id
