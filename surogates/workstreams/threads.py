"""Starting a project's thread: its session, its row, its goal and its queue."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from surogates.board.groups import ensure_group_and_inherit
from surogates.config import enqueue_session
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.provisioning import create_thread_session
from surogates.workstreams import thread_config
from surogates.workstreams.store import WorkstreamStore


async def start_thread(
    *,
    session_store: Any,
    session_factory: Any,
    redis: Any,
    master: Session,
    live_config: dict[str, Any] | None,
    title: str,
    goal: str,
    context: str,
    proposal: dict[str, str] | None = None,
) -> Session | None:
    """Start a thread of *master*'s project on *goal*; None when the project is archived.

    The thread is queued only once its goal is written, so nothing runs it
    before it has its row and its first message.  A thread the user starts
    from a proposal card (*proposal*: its ``proposal_id`` and ``key``) is
    news to the master, which reads it at its next model request; it does
    not wake the master, whose next turn comes with the thread's first
    report at the latest.
    """
    projects = WorkstreamStore(session_factory)
    project = await projects.get(
        UUID(master.config["workstream_id"]),
        org_id=master.org_id, agent_id=master.agent_id, user_id=master.user_id,
    )
    if project is None:
        return None
    config = thread_config(project.id, title=title, tier=project.thread_tier)
    # The master's board group, so verified notes reach sibling threads.
    await ensure_group_and_inherit(
        parent_session=master, session_store=session_store,
        child_config=config, live_parent_config=live_config,
    )
    thread = await create_thread_session(store=session_store, master=master, config=config)
    content = f"{goal}\n\n## Context\n{context}" if context else goal
    try:
        await projects.add_thread(thread.id, project.id, title)
        await session_store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": content})
        spawned = {"worker_id": str(thread.id), "title": title, "goal": goal[:500]}
        if proposal is not None:
            spawned.update(started_by="user", proposal_id=proposal["proposal_id"], key=proposal["key"])
        await session_store.emit_event(master.id, EventType.WORKER_SPAWNED, spawned)
    except BaseException:
        # A thread without its row, its goal or its card is never queued:
        # archived, it is neither listed nor left for a duplicate to shadow.
        await session_store.update_session_status(thread.id, "archived")
        raise
    if redis is not None:
        await enqueue_session(
            redis, org_id=str(thread.org_id), agent_id=thread.agent_id, session_id=thread.id,
        )
    return thread
