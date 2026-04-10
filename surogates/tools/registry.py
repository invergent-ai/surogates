"""Central tool registry for the Surogates agent platform.

Provides a self-contained registry that stores tool schemas, handlers,
and metadata.  Supports registration, lookup, OpenAI-format schema
export, and async dispatch with result truncation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """Describes a single tool's interface in JSON-Schema terms."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for the tool's input


@dataclass(slots=True)
class ToolEntry:
    """An entry in the tool registry pairing a schema with its handler."""

    name: str
    schema: ToolSchema
    handler: Callable[..., Any]  # async def handler(arguments: dict, **kwargs) -> str
    toolset: str  # logical grouping: "core", "terminal", "web", "memory", "skills"
    is_async: bool = True
    max_result_size: int = 50_000  # chars


class ToolRegistry:
    """Central tool registration.  One instance per worker.

    Not a singleton -- callers are expected to manage the lifecycle of
    the registry instance and inject it where needed.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        schema: ToolSchema,
        handler: Callable[..., Any],
        toolset: str = "core",
        is_async: bool = True,
        max_result_size: int = 50_000,
    ) -> None:
        """Register a tool by *name*.

        Raises :class:`ValueError` if a tool with the same name is
        already registered.
        """
        if name in self._entries:
            raise ValueError(f"Tool {name!r} is already registered")
        self._entries[name] = ToolEntry(
            name=name,
            schema=schema,
            handler=handler,
            toolset=toolset,
            is_async=is_async,
            max_result_size=max_result_size,
        )
        logger.debug("Registered tool %s (toolset=%s)", name, toolset)

    def deregister(self, name: str) -> None:
        """Remove a previously registered tool.

        Silently succeeds if the tool does not exist.
        """
        removed = self._entries.pop(name, None)
        if removed is not None:
            logger.debug("Deregistered tool %s", name)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolEntry | None:
        """Return the :class:`ToolEntry` for *name*, or ``None``."""
        return self._entries.get(name)

    def get_all(self) -> list[ToolEntry]:
        """Return every registered tool entry."""
        return list(self._entries.values())

    def has(self, name: str) -> bool:
        """Return ``True`` if *name* is registered."""
        return name in self._entries

    @property
    def tool_names(self) -> set[str]:
        """The set of all registered tool names."""
        return set(self._entries.keys())

    # ------------------------------------------------------------------
    # Schema export
    # ------------------------------------------------------------------

    def get_schemas(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        """Return OpenAI-format tool schemas for ``chat.completions.create(tools=...)``.

        If *names* is provided, only tools whose names appear in the set
        are included.  Otherwise all registered tools are returned.

        Each element looks like::

            {
                "type": "function",
                "function": {
                    "name": "...",
                    "description": "...",
                    "parameters": { ... }
                }
            }
        """
        schemas: list[dict[str, Any]] = []
        for entry in self._entries.values():
            if names is not None and entry.name not in names:
                continue
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": entry.schema.name,
                        "description": entry.schema.description,
                        "parameters": entry.schema.parameters,
                    },
                }
            )
        return schemas

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        name: str,
        arguments: str | dict[str, Any],
        **kwargs: Any,
    ) -> str:
        """Execute a tool by name.

        *arguments* may be a JSON string (as received from the LLM) or an
        already-parsed ``dict``.  The result is coerced to ``str`` and
        truncated to ``ToolEntry.max_result_size`` characters.

        Raises :class:`KeyError` if no tool with *name* is registered.
        """
        entry = self._entries.get(name)
        if entry is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        # Parse JSON string arguments if needed.
        parsed: dict[str, Any]
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                return json.dumps({"error": f"Invalid JSON arguments: {exc}"})
        else:
            parsed = arguments

        try:
            if entry.is_async:
                result = await entry.handler(parsed, **kwargs)
            else:
                result = entry.handler(parsed, **kwargs)
        except Exception as exc:
            logger.exception("Tool %s raised an exception", name)
            return json.dumps({"error": f"Tool execution failed: {exc}"})

        # Coerce to str and truncate.
        result_str = str(result) if not isinstance(result, str) else result
        if len(result_str) > entry.max_result_size:
            truncated_at = entry.max_result_size
            result_str = (
                result_str[:truncated_at]
                + f"\n\n[truncated at {truncated_at} chars]"
            )

        return result_str
