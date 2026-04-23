"""Prompt library -- loads markdown prompt fragments with YAML frontmatter.

Prompt fragments live as ``.md`` files under :data:`PROMPTS_ROOT` (bundled
with the ``surogates.harness`` package).  Each file has the shape::

    ---
    name: <identifier>
    description: <one-line summary>
    applies_when: <optional natural-language trigger>
    ---
    <body>

The loader strips the frontmatter, caches the parsed body per-path for the
lifetime of the process, and exposes typed accessors used by
:class:`~surogates.harness.prompt.PromptBuilder`.

Fragment problems (missing file, malformed frontmatter) are raised, not
logged.  Every worker/API entry point calls :meth:`PromptLibrary.validate`
at startup so typos fail the readiness probe rather than taking live
sessions down mid-turn.

This module is the sole read path for platform-shipped prompt text;
nothing else should embed prompt prose inline.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

PROMPTS_ROOT: Path = Path(__file__).parent / "prompts"

_FRONTMATTER_FENCE = "---"

# Fragments every worker/API pod must be able to load at boot.  Checked by
# :meth:`PromptLibrary.validate` so a missing or malformed fragment fails
# the readiness probe instead of surfacing as a per-turn crash.  Platform
# hints are intentionally excluded — unknown channels fall back to "no
# hint" at runtime, so a typo there is a soft miss, not a fatal error.
REQUIRED_KEYS: tuple[str, ...] = (
    "guidance/memory",
    "guidance/session_search",
    "guidance/skills",
    "guidance/expert",
    "guidance/artifact",
    "guidance/coordinator",
    "guidance/tool_use_enforcement",
    "models/openai",
    "models/google",
    "identity/default_personality",
    "identity/workspace_rules",
)


class PromptNotFoundError(KeyError):
    """Raised when a required prompt fragment is missing on disk."""


class PromptFrontmatterError(ValueError):
    """Raised when a fragment has malformed or unparseable YAML frontmatter.

    Examples: opening ``---`` with no closing fence, frontmatter that is
    not a YAML mapping, unparseable YAML syntax.  A fragment with no
    frontmatter at all is *not* an error — it is treated as a plain body.
    """


class PromptLibrary:
    """Reads markdown prompt fragments bundled with the harness package.

    Fragments are addressed by slash-separated keys that map directly onto
    the on-disk directory tree — e.g. ``guidance/memory`` resolves to
    ``prompts/guidance/memory.md``.  Platform hints are addressed via the
    dedicated :meth:`platform_hint` helper.

    Parsed bodies are cached by absolute path so repeated reads during a
    single process lifetime cost one disk hit per file.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root: Path = root or PROMPTS_ROOT
        self._body_cache: dict[Path, str] = {}
        self._meta_cache: dict[Path, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> str:
        """Return the body of the fragment at ``<root>/<key>.md``.

        Raises :class:`PromptNotFoundError` if the file is missing so that
        misconfiguration fails loudly at boot rather than producing silent
        empty prompts at runtime.
        """
        path = self._resolve(key)
        return self._load_body(path)

    def metadata(self, key: str) -> dict[str, Any]:
        """Return the parsed frontmatter for ``<root>/<key>.md``.

        Returns an empty dict when the file has no frontmatter.  Used by
        tooling that needs to introspect fragments (e.g. future evolution
        pipeline) without paying the body read cost.
        """
        path = self._resolve(key)
        self._load_body(path)  # populates meta cache as a side-effect
        return dict(self._meta_cache.get(path, {}))

    def platform_hint(self, channel: str) -> str | None:
        """Return the platform hint body for *channel*, or ``None``.

        Unlike :meth:`get`, missing platform hints are not errors: the
        caller tolerates unknown channels by simply omitting the hint.
        """
        if not channel:
            return None
        path = self._root / "platforms" / f"{channel}.md"
        if not path.is_file():
            return None
        return self._load_body(path)

    def platforms(self) -> dict[str, str]:
        """Return a ``{channel: body}`` mapping for every platform hint.

        Provided for test iteration and admin tooling; not used on the hot
        path.  Not cached as an aggregate because individual bodies are
        cached and the directory scan is cheap.
        """
        platforms_dir = self._root / "platforms"
        if not platforms_dir.is_dir():
            return {}
        result: dict[str, str] = {}
        for path in sorted(platforms_dir.glob("*.md")):
            result[path.stem] = self._load_body(path)
        return result

    def validate(self, required_keys: tuple[str, ...] = REQUIRED_KEYS) -> None:
        """Eagerly load every required fragment and every platform hint.

        Called once at worker/API startup.  Surfaces missing files,
        malformed frontmatter, and unreadable bodies as exceptions so
        the pod fails its readiness probe instead of crashing on the
        first prompt build.  Successful bodies stay in the cache so
        production requests hit memory, not disk.
        """
        for key in required_keys:
            self.get(key)
        # Sweep platform hints too -- individual misses are non-fatal at
        # runtime (unknown channels fall back silently), but malformed
        # frontmatter in a shipped hint is still a packaging bug and
        # should fail loud.
        platforms_dir = self._root / "platforms"
        if platforms_dir.is_dir():
            for path in sorted(platforms_dir.glob("*.md")):
                self._load_body(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve(self, key: str) -> Path:
        """Map ``"guidance/memory"`` to an absolute ``.md`` path."""
        path = self._root / f"{key}.md"
        if not path.is_file():
            raise PromptNotFoundError(
                f"prompt fragment not found: {key} (expected at {path})"
            )
        return path

    def _load_body(self, path: Path) -> str:
        """Read *path*, strip frontmatter, cache the body and metadata."""
        if path in self._body_cache:
            return self._body_cache[path]

        raw = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(raw)

        self._body_cache[path] = body
        self._meta_cache[path] = meta
        return body


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into ``(frontmatter_dict, body)``.

    Frontmatter is a YAML mapping delimited by ``---`` fence lines.
    A fragment with no opening fence is treated as a plain body and
    returns an empty dict.  A fragment that *starts* with a fence but
    is malformed (no closing fence, non-mapping YAML, unparseable YAML)
    raises :class:`PromptFrontmatterError` — this is always a typo, and
    silently shipping frontmatter as prompt text would be a subtle
    production bug.

    Line endings are normalized to ``\\n`` before parsing so that files
    saved with CRLF (Windows editors, pasted content) are handled the
    same as LF files.  Body whitespace is trimmed at both ends but
    internal formatting is preserved.
    """
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    stripped = raw.lstrip()
    if not stripped.startswith(_FRONTMATTER_FENCE):
        return {}, raw.strip()

    # Find the closing fence.  Search after the opening fence line.
    after_open = stripped[len(_FRONTMATTER_FENCE):]
    if not after_open.startswith("\n"):
        # Opening fence is not followed by a newline -- not actually
        # frontmatter, just a body that happens to begin with "---".
        return {}, raw.strip()
    remainder = after_open[1:]  # skip newline after opening fence

    # Handle the zero-length-metadata case ("---\n---\n..."): the closing
    # fence sits at the very start of `remainder` with no leading newline.
    if remainder.startswith(f"{_FRONTMATTER_FENCE}\n") or remainder == _FRONTMATTER_FENCE:
        fm_text = ""
        body = remainder[len(_FRONTMATTER_FENCE):].lstrip("\n")
    else:
        close_idx = remainder.find(f"\n{_FRONTMATTER_FENCE}")
        if close_idx == -1:
            raise PromptFrontmatterError(
                "opening '---' fence has no matching closing fence"
            )
        fm_text = remainder[:close_idx]
        body = remainder[close_idx + len(f"\n{_FRONTMATTER_FENCE}"):]

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise PromptFrontmatterError(
            f"frontmatter is not valid YAML: {exc}"
        ) from exc

    # An empty YAML block parses to None; accept it as "no metadata".
    if meta is None:
        meta = {}
    elif not isinstance(meta, dict):
        raise PromptFrontmatterError(
            f"frontmatter must be a YAML mapping, got {type(meta).__name__}"
        )

    return meta, body.strip()


# Module-level singleton for the default package-bundled library.  Tests
# and tools that want an isolated library instance should construct their
# own :class:`PromptLibrary` with a custom ``root``.
_default_library: PromptLibrary | None = None


def default_library() -> PromptLibrary:
    """Return a process-wide shared :class:`PromptLibrary` instance."""
    global _default_library
    if _default_library is None:
        _default_library = PromptLibrary()
    return _default_library
