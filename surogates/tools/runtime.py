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
        # Two builtins are intentionally NOT registered:
        #
        # - ``code_execution`` (``execute_code``): ``terminal`` plus inline
        #   ``python3 -c "..."`` covers every real use case, and exposing
        #   both tempts the LLM to retry the same logic via a different
        #   tool when one fails (observed with sandbox-provisioning errors).
        # - ``browser`` (``browser_navigate``): stub only — the schema
        #   advertises a capability the sandbox image (no Playwright /
        #   Chromium) cannot fulfil, so every call would fail.  Use
        #   ``web_search`` / ``web_extract`` / ``web_crawl`` instead.
        #
        # Both module files are preserved on disk so they can be
        # reinstated later without re-porting work.
        from surogates.tools.builtin import (
            clarify,
            coordinator,
            delegate,
            expert,
            file_ops,
            memory,
            session_search,
            skill_manager,
            skills,
            terminal,
            todo,
            web_search,
        )

        modules = [
            memory,
            skills,
            skill_manager,
            web_search,
            file_ops,
            delegate,
            terminal,  # also registers the 'process' tool
            session_search,
            todo,
            clarify,
            expert,
            coordinator,
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
