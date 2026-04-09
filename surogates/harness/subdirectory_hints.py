"""Subdirectory context hints -- lazy-loads AGENTS.md/CLAUDE.md as agent navigates.

When a tool call targets a new directory, checks for context files and appends
them to the tool result. This provides just-in-time context without bloating
the system prompt.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from typing import Any

from surogates.harness.context_files import scan_context_content

logger = logging.getLogger(__name__)

# Context files to look for in subdirectories.
HINT_FILENAMES: list[str] = [
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "claude.md",
    ".cursorrules",
]

MAX_HINT_CHARS: int = 8_000
MAX_ANCESTOR_WALK: int = 5

# Tool argument keys that typically contain file paths.
_PATH_ARG_KEYS: frozenset[str] = frozenset({"path", "file_path", "workdir"})

# Tools that take shell commands where we should extract paths.
_COMMAND_TOOLS: frozenset[str] = frozenset({"terminal"})


class SubdirectoryHintTracker:
    """Tracks visited directories and loads hints on first access.

    Usage::

        tracker = SubdirectoryHintTracker(initial_cwd="/project")

        # After each tool call:
        hints = tracker.check_tool_call("file_read", {"path": "backend/main.py"})
        if hints:
            tool_result += hints
    """

    def __init__(self, initial_cwd: str | None = None) -> None:
        self._visited: set[str] = set()
        if initial_cwd:
            resolved = str(Path(initial_cwd).resolve())
            self._visited.add(resolved)
        self._cwd: str = initial_cwd or "/"

    def check_tool_call(
        self, tool_name: str, tool_args: dict[str, Any]
    ) -> str | None:
        """Check tool args for new directories, return formatted hints or ``None``."""
        dirs = self._extract_directories(tool_name, tool_args)
        if not dirs:
            return None

        all_hints: list[str] = []
        for d in dirs:
            hint = self._load_hints(d)
            if hint:
                all_hints.append(hint)

        if not all_hints:
            return None

        return "\n\n" + "\n\n".join(all_hints)

    # ------------------------------------------------------------------
    # Directory extraction
    # ------------------------------------------------------------------

    def _extract_directories(
        self, tool_name: str, args: dict[str, Any]
    ) -> list[str]:
        """Extract directory paths from tool call arguments."""
        candidates: set[str] = set()

        for key in _PATH_ARG_KEYS:
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                self._add_path_candidate(val, candidates)

        if tool_name in _COMMAND_TOOLS:
            cmd = args.get("command", "")
            if isinstance(cmd, str):
                self._extract_paths_from_command(cmd, candidates)

        return list(candidates)

    def _add_path_candidate(self, raw_path: str, candidates: set[str]) -> None:
        """Resolve a path and add its directory + ancestors to candidates."""
        try:
            p = Path(raw_path).expanduser()
            if not p.is_absolute():
                p = Path(self._cwd) / p
            p = p.resolve()

            if p.suffix or (p.exists() and p.is_file()):
                p = p.parent

            for _ in range(MAX_ANCESTOR_WALK):
                resolved = str(p)
                if resolved in self._visited:
                    break
                if p.is_dir():
                    candidates.add(resolved)
                parent = p.parent
                if parent == p:
                    break
                p = parent
        except (OSError, ValueError):
            pass

    def _extract_paths_from_command(
        self, cmd: str, candidates: set[str]
    ) -> None:
        """Extract path-like tokens from a shell command string."""
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            tokens = cmd.split()

        for token in tokens:
            if token.startswith("-"):
                continue
            if "/" not in token and "." not in token:
                continue
            if token.startswith(("http://", "https://", "git@")):
                continue
            self._add_path_candidate(token, candidates)

    # ------------------------------------------------------------------
    # Hint loading
    # ------------------------------------------------------------------

    def _load_hints(self, directory: str) -> str | None:
        """Load hint files from *directory*. Returns formatted text or ``None``."""
        self._visited.add(directory)

        dir_path = Path(directory)
        if not dir_path.is_dir():
            return None

        for filename in HINT_FILENAMES:
            hint_path = dir_path / filename
            if not hint_path.is_file():
                continue
            try:
                content = hint_path.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                scanned = scan_context_content(content, filename)
                if scanned is None:
                    continue
                if len(scanned) > MAX_HINT_CHARS:
                    scanned = (
                        scanned[:MAX_HINT_CHARS]
                        + f"\n\n[...truncated {filename}: "
                        f"{len(scanned):,} chars total]"
                    )
                # First match wins per directory.
                logger.debug("Loaded subdirectory hint from %s", hint_path)
                return (
                    f"[Subdirectory context discovered: {hint_path}]\n{scanned}"
                )
            except Exception as exc:
                logger.debug("Could not read %s: %s", hint_path, exc)

        return None
