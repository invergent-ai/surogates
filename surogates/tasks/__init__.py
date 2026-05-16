"""Subagent task layer — durable, DAG-aware coordination on top of spawn_worker.

A Task is a persistent goal that can be executed by zero or more Session
attempts. The layer adds three things to the existing spawn_worker /
delegate_task / AgentDef infrastructure:

* **DAG dependencies** — a Task waits for every parent Task to reach
  ``done`` before its own Session is spawned.
* **Block / unblock** — a worker can self-pause via ``task_block`` and the
  spawning parent (or a human) can resume it with additional context.
* **Retry with history** — failed/crashed attempts are retried within
  ``max_attempts``; ``sessions.task_id`` links every attempt back to the
  task for full audit.

Public surface:

* ``surogates.tasks.tools.register(registry)`` — register the four task
  tools (``spawn_task``, ``unblock_task``, ``cancel_task``, ``task_block``)
  with a ``ToolRegistry`` instance.
* ``surogates.tasks.dispatcher.tasks_tick(...)`` — one tick of the
  promote/finalize/enqueue loop, called from the orchestrator at 5s
  cadence.

See ``docs/sub-agents/2026-05-16-subagent-task-layer-v1.md`` for the full
design.
"""
