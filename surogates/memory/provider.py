"""Abstract base class for pluggable memory providers.

Memory providers give the agent persistent recall across sessions.  
One external provider is active at a time alongside the always-on 
built-in memory (MEMORY.md / USER.md).

Lifecycle (called by :class:`MemoryManager`):

- ``initialize()``          -- connect, create resources, warm up
- ``system_prompt_block()`` -- static text for the system prompt
- ``prefetch(query)``       -- background recall before each turn
- ``sync_turn(user, asst)`` -- async write after each turn
- ``get_tool_schemas()``    -- tool schemas to expose to the model
- ``handle_tool_call()``    -- dispatch a tool call
- ``shutdown()``            -- clean exit

Optional hooks (override to opt in):

- ``on_turn_start()``
- ``on_session_end()``
- ``on_pre_compress(messages)`` -> ``str``
- ``on_memory_write(action, target, content)``
- ``on_delegation(task, result, child_session_id)``
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. ``'builtin'``, ``'honcho'``)."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the provider.

        Called once at agent startup.  May create resources, establish
        connections, start background threads, etc.
        """

    @abstractmethod
    def system_prompt_block(self) -> str | None:
        """Return text to include in the system prompt.

        Called during system prompt assembly.  Return ``None`` or empty
        string to skip.
        """

    @abstractmethod
    async def prefetch(self, query: str, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call.  Return formatted text to inject as
        context, or empty string if nothing relevant.
        """

    @abstractmethod
    async def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        session_id: str = "",
    ) -> None:
        """Persist a completed turn to the backend."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas this provider exposes.

        Each schema follows the OpenAI function calling format.
        Return empty list if this provider has no tools.
        """

    @abstractmethod
    async def handle_tool_call(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result).
        """

    # -- Optional hooks (override to opt in) ---------------------------------

    async def on_turn_start(self) -> None:
        """Called at the start of each turn."""

    async def on_session_end(self) -> None:
        """Called when a session ends."""

    async def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Return text to include in the compression summary prompt.
        Return empty string for no contribution.
        """
        return ""

    async def on_memory_write(
        self, action: str, target: str, content: str,
    ) -> None:
        """Called when the built-in memory tool writes an entry."""

    async def on_delegation(
        self,
        task: str,
        result: str,
        child_session_id: str = "",
    ) -> None:
        """Called on the parent agent when a subagent completes."""

    async def shutdown(self) -> None:
        """Clean shutdown -- flush queues, close connections."""
