"""Tool runtime bootstrap and lifecycle management.

:class:`ToolRuntime` wires the builtin tool modules into a
:class:`~surogates.tools.registry.ToolRegistry` and exposes a thin
dispatch facade.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolRuntime:
    """Bootstraps and manages the tool registry lifecycle.

    Typical usage::

        registry = ToolRegistry()
        runtime = ToolRuntime(registry)
        runtime.register_builtins()

        result = await runtime.dispatch("memory", {"action": "add", "target": "memory", "content": "project note"})
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def register_builtins(self) -> None:
        """Import and register every builtin tool module.

        Each module is expected to expose a ``register(registry)``
        function that calls ``registry.register(...)`` for each tool it
        provides.
        """
        # One builtin is intentionally NOT registered:
        #
        # - ``code_execution`` (``execute_code``): ``terminal`` plus inline
        #   ``python3 -c "..."`` covers every real use case, and exposing
        #   both tempts the LLM to retry the same logic via a different
        #   tool when one fails (observed with sandbox-provisioning errors).
        from surogates.tools.builtin import (
            artifact,
            ask_user_question,
            browser,
            coding_agent,
            coordinator,
            cron,
            delegate,
            expert,
            file_ops,
            kb_tools,
            loop_control,
            media_gen,
            memory,
            research,
            session_search,
            skill_manager,
            skills,
            terminal,
            todo,
            vision,
            web_search,
        )
        from surogates.board import tools as board_tools
        from surogates.tasks import tools as task_tools

        modules = [
            memory,
            research,
            skills,
            skill_manager,
            vision,
            media_gen,
            web_search,
            browser,
            file_ops,
            kb_tools,
            loop_control,
            delegate,
            expert,
            terminal,  # also registers the 'process' tool
            session_search,
            todo,
            ask_user_question,
            cron,
            coordinator,
            artifact,
            coding_agent,  # run_coding_agent (Claude Code / Codex)
            task_tools,  # spawn_task, unblock_task, cancel_task, worker_block/complete/context
            board_tools,  # share_note, read_board, expand_note (coordination board)
        ]

        for mod in modules:
            try:
                mod.register(self.registry)
                logger.debug("Registered builtin tools from %s", mod.__name__)
            except Exception:
                logger.exception(
                    "Failed to register builtin tools from %s", mod.__name__,
                )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        name: str,
        arguments: str | dict[str, Any],
        **kwargs: Any,
    ) -> str:
        """Delegate to :meth:`ToolRegistry.dispatch`."""
        return await self.registry.dispatch(name, arguments, **kwargs)
