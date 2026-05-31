"""Async sibling of MemoryStore that persists to R2.

Operates on an in-memory working set
populated from R2 at session start via :meth:`load_from_r2`.
Sync read APIs (``format_for_system_prompt``, ``get_entries``)
serve from the working set so ``PromptBuilder.build()`` stays
sync.  Async write APIs (``add``, ``replace``, ``remove``) update
the working set AND persist to R2.

Conflict detection: every write re-reads R2 before persisting
and compares the version to ``self.last_seen_version``.  On
mismatch, emit :attr:`~surogates.audit.types.AuditType.MEMORY_CONFLICT`;
this method observes the version delta and proceeds with last-
write-wins by writing at ``current_r2_version + 1``.

The in-memory working set is just a string — same shape the
legacy MemoryStore exposes through ``format_for_system_prompt``
— so the existing MemoryManager and the agent-facing memory tools
don't care which backend they got.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from surogates.runtime.memory_io import (
    read_user_memory, write_user_memory,
)

__all__ = ["R2MemoryStore"]


# Delimiter used between memory entries; matches the legacy
# disk-based MemoryStore so the on-the-wire content is
# interchangeable between modes.
_ENTRY_DELIMITER = "\n---\n"


class R2MemoryStore:
    """Per-session R2-backed memory store."""

    def __init__(
        self,
        *,
        backend: Any,
        bucket: str,
        key: str,
        on_write: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._backend = backend
        self._bucket = bucket
        self._key = key
        self._on_write = on_write
        self._content: str = ""
        self.last_seen_version: int = 0

    async def load_from_r2(self) -> None:
        """Read the user's memory from R2 into the in-memory working set.

        Called once at session start by the harness factory.  A
        missing-key result (new user, no prior memory) populates
        an empty working set at version 0.
        """
        result = await read_user_memory(
            self._backend, bucket=self._bucket, key=self._key,
        )
        if result is None:
            self._content = ""
            self.last_seen_version = 0
        else:
            self._content, self.last_seen_version = result

    def get_entries(self, target: str) -> list[str]:
        """Return the entries for ``target`` (sync; reads working set)."""
        if not self._content:
            return []
        return [
            entry for entry in self._content.split(_ENTRY_DELIMITER)
            if entry
        ]

    def format_for_system_prompt(self, target: str) -> str | None:
        """Render the working set into a single string for the
        PromptBuilder's memory section.  ``None`` when empty."""
        entries = self.get_entries(target)
        if not entries:
            return None
        return _ENTRY_DELIMITER.join(entries)

    async def add(self, target: str, content: str) -> dict[str, Any]:
        """Append ``content`` to the working set and persist."""
        if self._content:
            self._content = self._content + _ENTRY_DELIMITER + content
        else:
            self._content = content
        await self._persist(action="add")
        return {"target": target, "action": "add"}

    async def replace(
        self, target: str, old_text: str, new_content: str,
    ) -> dict[str, Any]:
        if old_text not in self._content:
            return {
                "target": target, "action": "replace",
                "error": "old_text not found",
            }
        self._content = self._content.replace(old_text, new_content, 1)
        await self._persist(action="replace")
        return {"target": target, "action": "replace"}

    async def remove(
        self, target: str, old_text: str,
    ) -> dict[str, Any]:
        if old_text not in self._content:
            return {
                "target": target, "action": "remove",
                "error": "old_text not found",
            }
        self._content = self._content.replace(old_text, "", 1)
        await self._persist(action="remove")
        return {"target": target, "action": "remove"}

    async def _persist(self, *, action: str) -> None:
        """Re-read R2 → detect conflict → write at version + 1.

        Conflict detection: if the on-R2 version diverges from
        ``self.last_seen_version``, another writer landed between
        load and write.  Proceeds with last-write-wins; if an
        ``on_write`` callback is wired, invokes it with
        ``conflict_detected=True`` so the harness can emit a
        ``MEMORY_CONFLICT`` audit event.
        """
        current = await read_user_memory(
            self._backend, bucket=self._bucket, key=self._key,
        )
        current_version = current[1] if current is not None else 0
        conflict = current_version != self.last_seen_version

        new_version = await write_user_memory(
            self._backend,
            bucket=self._bucket,
            key=self._key,
            content=self._content,
            expected_version=current_version,
        )
        self.last_seen_version = new_version

        if self._on_write is not None:
            await self._on_write(
                action,
                new_version=new_version,
                conflict_detected=conflict,
            )
