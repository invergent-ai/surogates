"""Programmatic task creation — shared by the ``spawn_task`` LLM tool and
``dispatch_experiments`` (research missions).

The body here is lifted verbatim from ``_spawn_task_handler`` (the
insert + atomic-claim + eager-spawn + enqueue core); the LLM tool keeps
its argument parsing and the calling-session mission read, and passes
already-validated values in. ``dispatch_experiments`` supplies the same
values directly. Keeping one implementation means both paths share the
exact race-safe claim semantics (UPDATE ... RETURNING, not FOR UPDATE)
and the same rollback-on-spawn-failure behaviour.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from sqlalchemy import func, select, update

from surogates.config import enqueue_session
from surogates.db.models import Task, TaskLink

logger = logging.getLogger(__name__)


class TaskSpawnError(Exception):
    """Raised for caller-correctable spawn failures (bad parents, config).

    The LLM tool surfaces ``str(exc)`` as a tool error; the dispatch tool
    surfaces it as a dispatch refusal. Internal/unexpected failures are
    logged and raised as this type with a generic message so neither
    caller leaks a stack trace to the model.
    """


async def create_task_and_spawn(
    *,
    goal: str,
    context: str | None,
    agent_def_name: str | None,
    max_attempts: int,
    parent_ids: list[UUID],
    parent_session_id: UUID,
    org_id: UUID,
    mission_id: UUID | None,
    session_store: object,
    session_factory: object,
    redis: object,
    tenant: object,
) -> dict[str, str]:
    """Insert a Task row and eagerly spawn its first attempt.

    Returns one of:
    * ``{"task_id", "status": "todo"}`` — parents still pending; the
      dispatcher tick will spawn once they complete.
    * ``{"task_id", "status": "ready"}`` — lost the eager-spawn race to
      the tick (work is already in flight or imminent).
    * ``{"task_id", "status": "running", "worker_id"}`` — spawned now.

    Raises :class:`TaskSpawnError` on bad parents / config / internal
    failure (the dispatcher tick retries the transient ones).
    """
    # ----- Phase 1: validate parents + insert Task row + links --------------
    try:
        async with session_factory() as db:
            initial_status = "ready"
            if parent_ids:
                parent_rows = (
                    await db.execute(
                        select(Task).where(Task.id.in_(parent_ids))
                    )
                ).scalars().all()
                if len(parent_rows) != len(parent_ids):
                    found = {p.id for p in parent_rows}
                    missing = [pid for pid in parent_ids if pid not in found]
                    raise TaskSpawnError(
                        f"parent task(s) not found: {', '.join(str(m) for m in missing)}"
                    )
                for p in parent_rows:
                    if p.org_id != org_id:
                        raise TaskSpawnError(
                            f"parent task {p.id} belongs to a different org; "
                            "cross-org dependencies are not allowed"
                        )
                if any(p.status != "done" for p in parent_rows):
                    initial_status = "todo"

            task = Task(
                org_id=org_id,
                parent_session_id=parent_session_id,
                agent_def_name=agent_def_name,
                goal=goal,
                context=context,
                status=initial_status,
                max_attempts=max_attempts,
                mission_id=mission_id,
            )
            db.add(task)
            await db.flush()
            for pid in parent_ids:
                db.add(TaskLink(parent_id=pid, child_id=task.id))
            await db.commit()
            task_id = task.id
    except TaskSpawnError:
        raise
    except Exception as exc:
        logger.exception("create_task_and_spawn: failed to insert Task row")
        raise TaskSpawnError("internal error creating task") from exc

    # If status is 'todo' we're done — the dispatcher tick will promote
    # and spawn once parents complete.
    if initial_status == "todo":
        return {"task_id": str(task_id), "status": "todo"}

    # ----- Phase 2: atomic claim via UPDATE ... RETURNING -------------------
    # We can't use SELECT ... FOR UPDATE here: the FOR UPDATE row lock on
    # the tasks row blocks the FK validation lock (FOR KEY SHARE) that
    # the subsequent INSERT into sessions(task_id=...) would need to
    # acquire, deadlocking the eager-spawn path. UPDATE ... RETURNING
    # serialises against concurrent UPDATEs (the dispatcher tick uses
    # the same WHERE status='ready' guard) without holding a lock past
    # the statement, so the child Session INSERT runs unblocked.
    try:
        async with session_factory() as db:
            claim_result = await db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "ready")
                .values(
                    status="running",
                    attempt_count=Task.attempt_count + 1,
                    started_at=func.coalesce(Task.started_at, func.now()),
                )
                .returning(Task)
                .execution_options(synchronize_session=False)
            )
            claimed = claim_result.scalar_one_or_none()
            await db.commit()

        if claimed is None:
            # Lost the race to the dispatcher tick — that's fine, the
            # work is already in-flight or imminently will be.
            return {"task_id": str(task_id), "status": "ready"}
    except Exception as exc:
        logger.exception("create_task_and_spawn: atomic claim failed for task %s", task_id)
        raise TaskSpawnError("internal error during atomic claim") from exc

    # We own the task. Spawn outside any DB transaction so the child
    # Session INSERT's FK lookup against tasks(id) doesn't conflict.
    from surogates.tasks.spawn import _create_session_for_task

    try:
        child = await _create_session_for_task(
            claimed,
            session_store=session_store,
            session_factory=session_factory,
            tenant=tenant,
        )
    except ValueError as exc:
        # Roll back the claim so the tick can retry once a human fixes
        # the AgentDef catalog. Decrement attempt_count because this
        # attempt never actually ran — counting it would burn a retry
        # budget on a config error.
        await _rollback_claim(session_factory, task_id)
        logger.warning(
            "create_task_and_spawn: agent_def_name resolution failed for task %s: %s",
            task_id, exc,
        )
        raise TaskSpawnError(str(exc)) from exc
    except Exception as exc:
        # Unknown error during spawn: same rollback pattern.
        logger.exception("create_task_and_spawn: unexpected error during child spawn")
        await _rollback_claim(session_factory, task_id)
        raise TaskSpawnError(
            "internal error during child spawn (tick will retry)"
        ) from exc

    # Wire the child session id back onto the Task row.
    try:
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(current_session_id=child.id)
            )
            await db.commit()
    except Exception:
        # The child Session exists but the Task row doesn't point at it.
        # The tick's finalize step will observe the orphan on the next
        # pass and recover by either matching the session or treating
        # the running attempt as crashed. Log and continue so the caller
        # still gets a useful result.
        logger.exception(
            "create_task_and_spawn: failed to wire current_session_id for task %s "
            "(child %s spawned but pointer not committed)",
            task_id, child.id,
        )

    await enqueue_session(
        redis,
        org_id=str(child.org_id),
        agent_id=child.agent_id,
        session_id=child.id,
    )
    return {
        "task_id": str(task_id),
        "status": "running",
        "worker_id": str(child.id),
    }


async def _rollback_claim(session_factory, task_id: UUID) -> None:
    """Undo a Phase-2 claim (status->ready, attempt_count-1) after a spawn
    failure so the dispatcher tick can retry without burning a retry."""
    try:
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(status="ready", attempt_count=Task.attempt_count - 1)
            )
            await db.commit()
    except Exception:
        logger.exception(
            "create_task_and_spawn: failed to roll back claim for task %s after "
            "spawn error; manual intervention may be required",
            task_id,
        )


def result_to_json(result: dict[str, str]) -> str:
    """Serialize a :func:`create_task_and_spawn` result for a tool reply."""
    return json.dumps(result)
