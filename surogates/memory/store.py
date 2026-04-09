"""MemoryStore -- bounded, file-backed persistent memory.

Provides the core I/O layer for two memory files:

- ``MEMORY.md`` -- agent's personal notes and observations
- ``USER.md``   -- what the agent knows about the user

Both are injected into the system prompt as a **frozen snapshot** captured
at :meth:`load_from_disk` time.  Mid-session writes update files on disk
immediately (durable) but do NOT change the system prompt -- this preserves
the prefix cache for the entire session.  The snapshot refreshes on the
next ``load_from_disk()`` call.

Entry delimiter: ``\n\u00a7\n`` (section sign).  Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n\u00a7\n"

# ---------------------------------------------------------------------------
# Memory content scanning -- lightweight check for injection/exfiltration
# in content that gets injected into the system prompt.
# ---------------------------------------------------------------------------

_MEMORY_THREAT_PATTERNS: list[tuple[str, str]] = [
    # Prompt injection
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    # Exfiltration via curl/wget with secrets
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    # Persistence via shell rc
    (r'authorized_keys', "ssh_backdoor"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env"),
    # Additional Hermes Agent patterns (pad to 16)
    (r'eval\s*\(', "eval_injection"),
    (r'exec\s*\(', "exec_injection"),
    (r'__import__\s*\(', "import_injection"),
    (r'subprocess\s*\.\s*(run|call|Popen|check_output)', "subprocess_injection"),
]

# Subset of invisible chars for injection detection.
_INVISIBLE_CHARS: set[str] = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


def scan_memory_content(content: str) -> str | None:
    """Scan memory content for injection/exfiltration patterns.

    Returns an error string if blocked, ``None`` if clean.
    """
    # Check invisible unicode.
    for char in _INVISIBLE_CHARS:
        if char in content:
            return (
                f"Blocked: content contains invisible unicode character "
                f"U+{ord(char):04X} (possible injection)."
            )

    # Check threat patterns.
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return (
                f"Blocked: content matches threat pattern '{pid}'. "
                f"Memory entries are injected into the system prompt and "
                f"must not contain injection or exfiltration payloads."
            )

    return None


# ---------------------------------------------------------------------------
# File locking -- fcntl on Unix, threading lock fallback elsewhere.
# ---------------------------------------------------------------------------

try:
    import fcntl as _fcntl  # type: ignore[import-not-found]
    _HAS_FCNTL = True
except ImportError:
    _fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

# Fallback lock for non-Unix systems (per-path granularity).
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _file_lock(path: Path):
    """Acquire an exclusive file lock for read-modify-write safety.

    Uses a separate ``.lock`` file so the memory file itself can still
    be atomically replaced via ``os.replace()``.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if _HAS_FCNTL:
        fd = open(lock_path, "w")
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
            fd.close()
    else:
        key = str(lock_path)
        with _THREAD_LOCKS_GUARD:
            if key not in _THREAD_LOCKS:
                _THREAD_LOCKS[key] = threading.Lock()
            lock = _THREAD_LOCKS[key]
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------


class MemoryStore:
    """Bounded curated memory with file persistence.

    Maintains two parallel states:

    - ``_system_prompt_snapshot`` -- frozen at load time, used for system
      prompt injection.  Never mutated mid-session.  Keeps prefix cache
      stable.
    - ``memory_entries`` / ``user_entries`` -- live state, mutated by tool
      calls, persisted to disk.  Tool responses always reflect this live
      state.
    """

    def __init__(
        self,
        memory_dir: Path,
        memory_char_limit: int = 2200,
        user_char_limit: int = 1375,
    ) -> None:
        self._memory_dir = Path(memory_dir)
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for system prompt -- set once at load_from_disk().
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}

    # -- Public API ---------------------------------------------------------

    def load_from_disk(self) -> None:
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(self._memory_dir / "MEMORY.md")
        self.user_entries = self._read_file(self._memory_dir / "USER.md")

        # Deduplicate entries (preserves order, keeps first occurrence).
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        # Capture frozen snapshot for system prompt injection.
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user": self._render_block("user", self.user_entries),
        }

    def add(self, target: str, content: str) -> dict[str, Any]:
        """Append a new entry.  Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        scan_error = scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with _file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            # Reject exact duplicates.
            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            # Calculate what the new total would be.
            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return {
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Replace or remove existing entries first."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                }

            entries.append(content)
            self._set_entries(target, entries)
            self._save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """Find entry containing *old_text* substring, replace it with *new_content*."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete entries."}

        scan_error = scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with _file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            limit = self._char_limit(target)

            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                return {
                    "success": False,
                    "error": (
                        f"Replacement would put memory at {new_total:,}/{limit:,} chars. "
                        f"Shorten the new content or remove other entries first."
                    ),
                }

            entries[idx] = new_content
            self._set_entries(target, entries)
            self._save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """Remove the entry containing *old_text* substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with _file_lock(self._path_for(target)):
            self._reload_target(target)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return {"success": False, "error": f"No entry matched '{old_text}'."}

            if len(matches) > 1:
                unique_texts = set(e for _, e in matches)
                if len(unique_texts) > 1:
                    previews = [e[:80] + ("..." if len(e) > 80 else "") for _, e in matches]
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": previews,
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self._save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def format_for_system_prompt(self, target: str) -> str | None:
        """Return the frozen snapshot for system prompt injection.

        Returns the state captured at :meth:`load_from_disk` time, NOT the
        live state.  Mid-session writes do not affect this.  This keeps the
        system prompt stable across all turns, preserving the prefix cache.

        Returns ``None`` if the snapshot is empty (no entries at load time).
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block if block else None

    def get_entries(self, target: str) -> list[str]:
        """Return the live entries for *target*."""
        return list(self._entries_for(target))

    def get_usage(self, target: str) -> dict[str, Any]:
        """Return usage stats for *target*."""
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        return {
            "usage_chars": current,
            "max_chars": limit,
            "usage_pct": pct,
            "entry_count": len(self._entries_for(target)),
        }

    # -- Internal helpers ---------------------------------------------------

    def _path_for(self, target: str) -> Path:
        if target == "user":
            return self._memory_dir / "USER.md"
        return self._memory_dir / "MEMORY.md"

    def _reload_target(self, target: str) -> None:
        """Re-read entries from disk into in-memory state.

        Called under file lock to get the latest state before mutating.
        """
        fresh = self._read_file(self._path_for(target))
        fresh = list(dict.fromkeys(fresh))
        self._set_entries(target, fresh)

    def _save_to_disk(self, target: str) -> None:
        """Persist entries to the appropriate file.  Called after every mutation."""
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> list[str]:
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def _char_limit(self, target: str) -> int:
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit

    def _success_response(self, target: str, message: str | None = None) -> dict[str, Any]:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp: dict[str, Any] = {
            "success": True,
            "target": target,
            "entries": entries,
            "entry_count": len(entries),
            "usage_chars": current,
            "max_chars": limit,
            "usage_pct": pct,
        }
        if message:
            resp["message"] = message
        return resp

    def _render_block(self, target: str, entries: list[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""

        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% \u2014 {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% \u2014 {current:,}/{limit:,} chars]"

        separator = "\u2550" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        """Read a memory file and split into entries.

        No file locking needed: :meth:`_write_file` uses atomic rename, so
        readers always see either the previous complete file or the new one.
        """
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return []

        if not raw.strip():
            return []

        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        """Write entries to a memory file using atomic temp-file + rename.

        Uses ``os.replace()`` for atomicity -- readers always see either
        the old complete file or the new one.
        """
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".mem_",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(path))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}") from e
