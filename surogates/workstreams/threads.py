"""A project's threads: starting one (its session, its row, its goal and its
queue), stopping one, and reopening one the user gives new work."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from surogates.board.groups import ensure_group_and_inherit
from surogates.config import INTERRUPT_CHANNEL_PREFIX, enqueue_session
from surogates.devices.store import DeviceRecord
from surogates.session.events import EventType
from surogates.session.models import Session
from surogates.session.provisioning import create_thread_session
from surogates.workstreams import is_project_thread, thread_config
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
    thread = await make_thread(
        session_store=session_store, session_factory=session_factory, master=master,
        live_config=live_config, title=title,
    )
    if thread is None:
        return None
    return await begin_thread(
        session_store=session_store, session_factory=session_factory, redis=redis, master=master,
        thread=thread, title=title, goal=goal, context=context, proposal=proposal,
    )


async def make_thread(
    *,
    session_store: Any,
    session_factory: Any,
    master: Session,
    live_config: dict[str, Any] | None,
    title: str,
    device: DeviceRecord | None = None,
    folder: str | None = None,
    card: dict[str, str] | None = None,
) -> Session | None:
    """A new thread of *master*'s project, not begun: no row, no goal, not
    queued.  None when the project is archived.

    With *device*, a computer of the user's, it works in *folder* there, and
    *card*, the proposal's ``proposal_id`` and ``key``, names what it begins
    with once that computer has bound it.
    """
    project = await WorkstreamStore(session_factory).get(
        UUID(master.config["workstream_id"]),
        org_id=master.org_id, agent_id=master.agent_id, user_id=master.user_id,
    )
    if project is None:
        return None
    config = thread_config(project, title=title)
    if card is not None:
        config["workstream_card"] = card
    # The master's board group, so verified notes reach sibling threads.
    await ensure_group_and_inherit(
        parent_session=master, session_store=session_store,
        child_config=config, live_parent_config=live_config,
    )
    return await create_thread_session(
        store=session_store, master=master, config=config,
        device_id=device.id if device is not None else None,
        device_name=device.name if device is not None else None,
        folder=folder,
    )


async def begin_thread(
    *,
    session_store: Any,
    session_factory: Any,
    redis: Any,
    master: Session,
    thread: Session,
    title: str,
    goal: str,
    context: str,
    proposal: dict[str, str] | None = None,
) -> Session | None:
    """Give *thread* its row and its goal, tell the master, and queue it;
    None, the thread archived, when its project was archived meanwhile."""
    content = f"{goal}\n\n## Context\n{context}" if context else goal
    try:
        if not await WorkstreamStore(session_factory).add_thread(thread.id, UUID(master.config["workstream_id"]), title):
            # Archived since it was read: the thread goes with the project.
            await session_store.update_session_status(thread.id, "archived")
            return None
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


async def reopen_if_thread(session: Session, *, app_state: Any) -> None:
    """The user's new work for a project's thread takes it out of Resolved.

    *app_state* is the API's ``app.state``; its ``session_factory`` is read
    only for a thread, so a chat outside projects needs none.
    """
    if is_project_thread(session.config):
        await WorkstreamStore(app_state.session_factory).reopen_thread(session.id)


async def stop_thread(
    thread: Session, *, reason: str, interrupt: str, session_store: Any, session_factory: Any, redis: Any,
) -> bool:
    """Pause *thread* if it is working, and interrupt its turn; whether it was working.

    The status first, so the thread reads as stopped, then the interrupt
    that ends its turn.  A thread already paused is interrupted again: its
    turn may not have heard the first time.  Unlike the pause route, it
    leaves the device operations of the thread's tree alone.  *reason*
    goes into its ``session.pause``; *interrupt* is always the server's
    words, never a model's, because the dispatcher reads some reasons as
    commands (``_SESSION_GONE_REASONS``).
    """
    stopped = await WorkstreamStore(session_factory).pause_thread(thread.id)
    if stopped:
        await session_store.emit_event(thread.id, EventType.SESSION_PAUSE, {"reason": reason})
    if redis is not None and (stopped or thread.status == "paused"):
        await redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{thread.id}", json.dumps({"reason": interrupt}))
    return stopped
