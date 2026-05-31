"""Async sibling of MemoryStore that persists to R2.

One R2 object per ``(user, target)`` tuple so the two memory targets
(``"memory"`` — agent-facing notes, ``"user"`` — facts about the
user) don't trample each other.  Both targets share a single
``R2MemoryStore`` instance; the working set is a mapping keyed by
target.

Sync read APIs (``format_for_system_prompt``, ``get_entries``)
serve from the in-memory working set so ``PromptBuilder.build()``
stays sync.  Async write APIs (``add``, ``replace``, ``remove``)
update the working set AND persist to R2.

Conflict detection: every write re-reads R2 before persisting and
compares the version to the last-seen version for that target.  On
mismatch, the ``on_write`` callback is invoked with
``conflict_detected=True`` so the harness can emit a memory-conflict
audit; the store then proceeds with last-write-wins by writing at
``current_r2_version + 1``.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from surogates.runtime.memory_io import (
    read_user_memory, write_user_memory,
)

__all__ = ["R2MemoryStore", "MEMORY_TARGETS"]


# Delimiter used between memory entries; matches the legacy
# disk-based MemoryStore so the on-the-wire content is
# interchangeable between modes.
_ENTRY_DELIMITER = "\n---\n"

#: Memory targets the store accepts.  Anything else raises at the API
#: boundary so a typo doesn't silently land into a third invisible blob.
MEMORY_TARGETS: tuple[str, ...] = ("memory", "user")


class R2MemoryStore:
    """Per-session R2-backed memory store, one R2 object per target."""

    def __init__(
        self,
        *,
        backend: Any,
        bucket: str,
        keys: dict[str, str],
        on_write: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        """Construct a store backed by the given R2 keys.

        ``keys`` maps target name (``"memory"`` / ``"user"``) to its R2
        object key.  All targets in :data:`MEMORY_TARGETS` must be
        present; missing entries raise ``KeyError`` so a wiring bug
        surfaces at construction rather than first write.
        """
        missing = [t for t in MEMORY_TARGETS if t not in keys]
        if missing:
            raise KeyError(
                f"R2MemoryStore is missing keys for targets: {missing}",
            )
        self._backend = backend
        self._bucket = bucket
        self._keys: dict[str, str] = dict(keys)
        self._on_write = on_write
        self._content: dict[str, str] = {t: "" for t in MEMORY_TARGETS}
        self._last_seen_version: dict[str, int] = {
            t: 0 for t in MEMORY_TARGETS
        }

    async def load_from_r2(self) -> None:
        """Read every target's blob from R2 into the working set.

        Called once at session start by the harness factory.  A
        missing-key result (new user, no prior memory for this target)
        populates an empty working set at version 0 for that target.
        """
        for target in MEMORY_TARGETS:
            result = await read_user_memory(
                self._backend, bucket=self._bucket, key=self._keys[target],
            )
            if result is None:
                self._content[target] = ""
                self._last_seen_version[target] = 0
            else:
                self._content[target], self._last_seen_version[target] = result

    # ── Read APIs (sync, served from the working set) ──────────────

    def _check_target(self, target: str) -> None:
        if target not in MEMORY_TARGETS:
            raise ValueError(
                f"unknown memory target: {target!r}; expected one of "
                f"{MEMORY_TARGETS}",
            )

    def get_entries(self, target: str) -> list[str]:
        """Return the entries for ``target`` (sync; reads working set)."""
        self._check_target(target)
        content = self._content[target]
        if not content:
            return []
        return [entry for entry in content.split(_ENTRY_DELIMITER) if entry]

    def format_for_system_prompt(self, target: str) -> str | None:
        """Render ``target``'s working set into a single string for the
        PromptBuilder's memory section.  ``None`` when empty."""
        entries = self.get_entries(target)
        if not entries:
            return None
        return _ENTRY_DELIMITER.join(entries)

    @property
    def last_seen_version(self) -> int:
        """Backwards-compatible single-version reader; reports the
        version of the ``"memory"`` target since that's what the
        original single-target store carried."""
        return self._last_seen_version["memory"]

    # ── Write APIs ─────────────────────────────────────────────────

    async def add(self, target: str, content: str) -> dict[str, Any]:
        """Append ``content`` to ``target``'s working set and persist."""
        self._check_target(target)
        current = self._content[target]
        self._content[target] = (
            current + _ENTRY_DELIMITER + content if current else content
        )
        await self._persist(target=target, action="add")
        return {"target": target, "action": "add"}

    async def replace(
        self, target: str, old_text: str, new_content: str,
    ) -> dict[str, Any]:
        self._check_target(target)
        if old_text not in self._content[target]:
            return {
                "target": target, "action": "replace",
                "error": "old_text not found",
            }
        self._content[target] = self._content[target].replace(
            old_text, new_content, 1,
        )
        await self._persist(target=target, action="replace")
        return {"target": target, "action": "replace"}

    async def remove(
        self, target: str, old_text: str,
    ) -> dict[str, Any]:
        self._check_target(target)
        if old_text not in self._content[target]:
            return {
                "target": target, "action": "remove",
                "error": "old_text not found",
            }
        self._content[target] = self._content[target].replace(
            old_text, "", 1,
        )
        await self._persist(target=target, action="remove")
        return {"target": target, "action": "remove"}

    async def _persist(self, *, target: str, action: str) -> None:
        """Re-read R2 → detect conflict → write at version + 1 for ``target``.

        Conflict detection: if the on-R2 version diverges from the
        last-seen version for this target, another writer landed
        between load and write.  Proceeds with last-write-wins; if an
        ``on_write`` callback is wired, invokes it with
        ``conflict_detected=True`` so the harness can emit a memory-
        conflict audit event.
        """
        key = self._keys[target]
        current = await read_user_memory(
            self._backend, bucket=self._bucket, key=key,
        )
        current_version = current[1] if current is not None else 0
        conflict = current_version != self._last_seen_version[target]

        new_version = await write_user_memory(
            self._backend,
            bucket=self._bucket,
            key=key,
            content=self._content[target],
            expected_version=current_version,
        )
        self._last_seen_version[target] = new_version

        if self._on_write is not None:
            await self._on_write(
                action,
                target=target,
                new_version=new_version,
                conflict_detected=conflict,
            )
