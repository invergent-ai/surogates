"""Shared validation helpers for skill operations.

These pure functions validate skill names, categories, frontmatter,
content size, and file paths.  Used by both the ``skill_manage`` tool
handler and the Skills REST API.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
MAX_SKILL_FILE_BYTES = 1_048_576  # 1 MiB per supporting file

VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ALLOWED_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})


# ---------------------------------------------------------------------------
# Validators — return error message or None
# ---------------------------------------------------------------------------


def validate_name(name: str) -> str | None:
    """Return an error message if *name* is invalid, else ``None``."""
    if not name:
        return "Skill name is required."
    if len(name) > MAX_NAME_LENGTH:
        return f"Skill name exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(name):
        return (
            f"Invalid skill name '{name}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Must start with a letter or digit."
        )
    return None


def validate_category(category: str | None) -> str | None:
    """Return an error message if *category* is invalid, else ``None``."""
    if category is None:
        return None
    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores. Categories must be a single directory name."
        )
    return None


def validate_frontmatter(content: str) -> str | None:
    """Return an error if frontmatter is missing or invalid, else ``None``."""
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."

    yaml_content = content[3 : end_match.start() + 3]

    try:
        import yaml

        parsed = yaml.safe_load(yaml_content)
    except Exception as exc:
        return f"YAML frontmatter parse error: {exc}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed.get("description", ""))) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3 :].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."

    return None


def validate_content_size(content: str, label: str = "SKILL.md") -> str | None:
    """Return an error if content exceeds the size limit."""
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,}). "
            f"Consider splitting into a smaller SKILL.md with supporting files "
            f"in references/ or templates/."
        )
    return None


def validate_file_path(file_path: str) -> str | None:
    """Return an error if *file_path* is invalid for write_file/remove_file."""
    if not file_path:
        return "file_path is required."

    normalized = Path(file_path)

    if ".." in normalized.parts:
        return "Path traversal ('..') is not allowed."

    if not normalized.parts or normalized.parts[0] not in ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(ALLOWED_SUBDIRS))
        return f"File must be under one of: {allowed}. Got: '{file_path}'"

    if len(normalized.parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{normalized.parts[0]}/myfile.md'"

    return None


def validate_file_size(content: str) -> str | None:
    """Return an error if file content exceeds the byte limit."""
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return (
            f"File content is {content_bytes:,} bytes "
            f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes)."
        )
    return None
