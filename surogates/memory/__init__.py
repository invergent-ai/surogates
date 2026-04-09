"""Memory system for the Surogates platform.

Public API:

- :class:`MemoryStore`           -- core file I/O layer
- :class:`MemoryProvider`        -- abstract base for pluggable providers
- :class:`BuiltinMemoryProvider` -- wraps MemoryStore as a provider
- :class:`MemoryManager`         -- orchestrator (builtin + optional external)
"""

from __future__ import annotations

from surogates.memory.builtin import BuiltinMemoryProvider
from surogates.memory.manager import MemoryManager, build_memory_context_block
from surogates.memory.provider import MemoryProvider
from surogates.memory.store import MemoryStore

__all__ = [
    "BuiltinMemoryProvider",
    "MemoryManager",
    "MemoryProvider",
    "MemoryStore",
    "build_memory_context_block",
]
