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

        result = await runtime.dispatch("memory_read", {"key": "project"})
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
        from surogates.tools.builtin import delegate, file_ops, memory, skill_manager, skills, web_search

        modules = [memory, skills, skill_manager, web_search, file_ops, delegate]

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
