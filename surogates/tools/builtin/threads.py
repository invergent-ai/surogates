"""A project's thread tools: how its master gives out the project's work.

A thread is a worker session of the master that does one piece of the work
in a sandbox of its own and reports back at the end of every turn, as
``worker.complete``.  Only a project's master is sent these tools
(``PROJECT_THREAD_TOOLS``).
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any
from uuid import UUID

from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.workstreams import is_project_master

# The chat title's cap, since a thread's title is its chat's title.  Counted
# in UTF-16 units, as a project's name is and as the desktop shell counts.
_MAX_TITLE = 256

_START_THREAD_SCHEMA = ToolSchema(
    name="start_thread",
    description=(
        "Start a thread: a separate agent session that does one piece of the "
        "project's work in its own workspace and reports back to you when it "
        "has done it. Returns at once with the thread's id. Start several "
        "threads in one response for several unrelated tasks."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "A short name for the work, shown to the user on the thread's card.",
            },
            "goal": {
                "type": "string",
                "description": (
                    "What the thread must do, complete and self-contained. A thread "
                    "cannot see this conversation: give it the facts, the files to "
                    "use, and what done looks like."
                ),
            },
            "context": {
                "type": "string",
                "description": "Background the thread needs beyond the goal.",
            },
        },
        "required": ["title", "goal"],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    for name, schema, handler in (
        ("start_thread", _START_THREAD_SCHEMA, _start_thread_handler),
    ):
        registry.register(name=name, schema=schema, handler=handler, toolset="core")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


async def _start_thread_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from surogates.workstreams.threads import start_thread

    texts = {name: _text(arguments, name) for name in ("title", "goal", "context")}
    if not texts["title"]:
        return _error("title is required")
    # The title is the thread's session instructions and its report's header,
    # so a line break would let it write instructions of its own.
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in texts["title"]):
        return _error("title must be one line")
    if len(texts["title"].encode("utf-16-le")) // 2 > _MAX_TITLE:
        return _error(f"title must be at most {_MAX_TITLE} characters")
    if not texts["goal"]:
        return _error("goal is required")

    session_store = kwargs["session_store"]
    master = await session_store.get_session(UUID(str(kwargs["session_id"])))
    if not is_project_master(master.config):
        return _error("Only a project's coordinator starts threads.")
    thread = await start_thread(
        session_store=session_store,
        session_factory=kwargs["session_factory"],
        redis=kwargs.get("redis"),
        master=master,
        live_config=kwargs.get("session_config"),
        **texts,
    )
    if thread is None:
        return _error("This project is archived.")
    return json.dumps({"thread_id": str(thread.id), "title": texts["title"], "status": "started"})
