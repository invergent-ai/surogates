"""Builtin skill management tool -- agent-facing CRUD for skills.

Allows the agent to create, edit, patch, delete skills and manage supporting files within
skill directories.

Skills are stored in the user's tenant asset root:
    {asset_root}/{org_id}/users/{user_id}/skills/{name}/SKILL.md
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
MAX_SKILL_FILE_BYTES = 1_048_576  # 1 MiB per supporting file

VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ALLOWED_SUBDIRS = frozenset({"references", "templates", "scripts", "assets"})

SCHEMA = ToolSchema(
    name="skill_manage",
    description=(
        "Create, edit, or delete skills. Skills are reusable instruction sets "
        "that capture procedural knowledge.\n\n"
        "Actions:\n"
        "- create: Create a new skill\n"
        "- edit: Rewrite a skill's SKILL.md\n"
        "- patch: Find-and-replace within a skill\n"
        "- delete: Remove a skill\n"
        "- write_file: Add a supporting file\n"
        "- remove_file: Remove a supporting file"
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "edit",
                    "patch",
                    "delete",
                    "write_file",
                    "remove_file",
                ],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars)."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "SKILL.md content for create/edit (YAML frontmatter + body)."
                ),
            },
            "category": {
                "type": "string",
                "description": "Optional category subdirectory for create.",
            },
            "old_string": {
                "type": "string",
                "description": "Text to find for patch.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text for patch.",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path for write_file/remove_file (must be under "
                    "references/templates/scripts/assets/)."
                ),
            },
            "file_content": {
                "type": "string",
                "description": "Content for write_file.",
            },
        },
        "required": ["action", "name"],
    },
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register the skill_manage tool."""
    registry.register(
        name="skill_manage",
        schema=SCHEMA,
        handler=_skill_manage_handler,
        toolset="skills",
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _skill_manage_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Dispatch to the appropriate action handler."""
    tenant = kwargs.get("tenant")
    if tenant is None:
        return json.dumps({"success": False, "error": "No tenant context available"})

    action = arguments.get("action", "")
    name = arguments.get("name", "")

    if action == "create":
        content = arguments.get("content")
        if not content:
            return json.dumps({
                "success": False,
                "error": "content is required for 'create'.",
            })
        category = arguments.get("category")
        result = _create_skill(name, content, tenant, category)

    elif action == "edit":
        content = arguments.get("content")
        if not content:
            return json.dumps({
                "success": False,
                "error": "content is required for 'edit'.",
            })
        result = _edit_skill(name, content, tenant)

    elif action == "patch":
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not old_string:
            return json.dumps({
                "success": False,
                "error": "old_string is required for 'patch'.",
            })
        if new_string is None:
            return json.dumps({
                "success": False,
                "error": "new_string is required for 'patch'.",
            })
        result = _patch_skill(name, old_string, new_string, tenant)

    elif action == "delete":
        result = _delete_skill(name, tenant)

    elif action == "write_file":
        file_path = arguments.get("file_path")
        file_content = arguments.get("file_content")
        if not file_path:
            return json.dumps({
                "success": False,
                "error": "file_path is required for 'write_file'.",
            })
        if file_content is None:
            return json.dumps({
                "success": False,
                "error": "file_content is required for 'write_file'.",
            })
        result = _write_file(name, file_path, file_content, tenant)

    elif action == "remove_file":
        file_path = arguments.get("file_path")
        if not file_path:
            return json.dumps({
                "success": False,
                "error": "file_path is required for 'remove_file'.",
            })
        result = _remove_file(name, file_path, tenant)

    else:
        result = {
            "success": False,
            "error": (
                f"Unknown action '{action}'. "
                "Use: create, edit, patch, delete, write_file, remove_file"
            ),
        }

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str | None:
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


def _validate_category(category: str | None) -> str | None:
    """Return an error message if *category* is invalid, else ``None``."""
    if category is None:
        return None
    category = category.strip()
    if not category:
        return None
    if "/" in category or "\\" in category:
        return f"Invalid category '{category}'. Must be a single directory name."
    if len(category) > MAX_NAME_LENGTH:
        return f"Category exceeds {MAX_NAME_LENGTH} characters."
    if not VALID_NAME_RE.match(category):
        return (
            f"Invalid category '{category}'. Use lowercase letters, numbers, "
            "hyphens, dots, and underscores."
        )
    return None


def _validate_frontmatter(content: str) -> str | None:
    """Return an error if frontmatter is missing or invalid, else ``None``."""
    if not content.strip():
        return "Content cannot be empty."

    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---)."

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed."

    yaml_content = content[3 : end_match.start() + 3]

    try:
        import yaml

        parsed = yaml.safe_load(yaml_content)
    except Exception as exc:
        return f"YAML frontmatter parse error: {exc}"

    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping."

    if "name" not in parsed:
        return "Frontmatter must include 'name' field."
    if "description" not in parsed:
        return "Frontmatter must include 'description' field."
    if len(str(parsed.get("description", ""))) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."

    body = content[end_match.end() + 3 :].strip()
    if not body:
        return "SKILL.md must have content after the frontmatter."

    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> str | None:
    """Return an error if content exceeds the size limit."""
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"{label} content is {len(content):,} characters "
            f"(limit: {MAX_SKILL_CONTENT_CHARS:,})."
        )
    return None


def _validate_file_path(file_path: str) -> str | None:
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


# ---------------------------------------------------------------------------
# Skill directory helpers
# ---------------------------------------------------------------------------


def _user_skills_dir(tenant: Any) -> Path:
    """Return the user-scoped skills directory for a tenant."""
    return (
        Path(tenant.asset_root)
        / str(tenant.org_id)
        / "users"
        / str(tenant.user_id)
        / "skills"
    )


def _find_skill(name: str, tenant: Any) -> dict[str, Any] | None:
    """Find a skill by name across user, org-shared, and platform layers.

    Returns ``{"path": Path}`` or ``None``.
    """
    from surogates.tools.loader import PLATFORM_SKILLS_DIR

    asset_root = Path(tenant.asset_root)
    org_id = str(tenant.org_id)
    user_id = str(tenant.user_id)

    search_dirs = [
        asset_root / org_id / "users" / user_id / "skills",
        asset_root / org_id / "shared" / "skills",
        Path(PLATFORM_SKILLS_DIR),
    ]

    for skills_dir in search_dirs:
        if not skills_dir.is_dir():
            continue
        # Direct match: skills_dir/name/SKILL.md
        candidate = skills_dir / name / "SKILL.md"
        if candidate.is_file():
            return {"path": candidate.parent}
        # Category match: skills_dir/*/name/SKILL.md
        for subdir in skills_dir.iterdir():
            if subdir.is_dir():
                candidate = subdir / name / "SKILL.md"
                if candidate.is_file():
                    return {"path": candidate.parent}
    return None


def _resolve_skill_dir(name: str, tenant: Any, category: str | None = None) -> Path:
    """Build the directory path for a new skill."""
    base = _user_skills_dir(tenant)
    if category:
        return base / category / name
    return base / name


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write *content* to *path* using temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.tmp.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------


def _create_skill(
    name: str,
    content: str,
    tenant: Any,
    category: str | None = None,
) -> dict[str, Any]:
    """Create a new user skill."""
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}

    err = _validate_category(category)
    if err:
        return {"success": False, "error": err}

    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, tenant)
    if existing:
        return {
            "success": False,
            "error": f"A skill named '{name}' already exists at {existing['path']}.",
        }

    skill_dir = _resolve_skill_dir(name, tenant, category)
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    _atomic_write(skill_md, content)

    result: dict[str, Any] = {
        "success": True,
        "message": f"Skill '{name}' created.",
        "path": str(skill_dir),
    }
    if category:
        result["category"] = category
    return result


def _edit_skill(name: str, content: str, tenant: Any) -> dict[str, Any]:
    """Replace the SKILL.md of an existing skill."""
    err = _validate_frontmatter(content)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(content)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, tenant)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found.",
        }

    skill_md = existing["path"] / "SKILL.md"
    _atomic_write(skill_md, content)

    return {
        "success": True,
        "message": f"Skill '{name}' updated.",
        "path": str(existing["path"]),
    }


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str,
    tenant: Any,
) -> dict[str, Any]:
    """Simple find-and-replace within SKILL.md."""
    existing = _find_skill(name, tenant)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    target = existing["path"] / "SKILL.md"
    if not target.exists():
        return {"success": False, "error": "SKILL.md not found."}

    content = target.read_text(encoding="utf-8")

    count = content.count(old_string)
    if count == 0:
        preview = content[:500] + ("..." if len(content) > 500 else "")
        return {
            "success": False,
            "error": "old_string not found in SKILL.md.",
            "file_preview": preview,
        }

    new_content = content.replace(old_string, new_string)

    err = _validate_content_size(new_content)
    if err:
        return {"success": False, "error": err}

    _atomic_write(target, new_content)

    return {
        "success": True,
        "message": (
            f"Patched SKILL.md in skill '{name}' "
            f"({count} replacement{'s' if count > 1 else ''})."
        ),
    }


def _delete_skill(name: str, tenant: Any) -> dict[str, Any]:
    """Delete a skill directory."""
    existing = _find_skill(name, tenant)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]
    shutil.rmtree(skill_dir)

    # Clean up empty category directory.
    parent = skill_dir.parent
    user_skills = _user_skills_dir(tenant)
    if parent != user_skills and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"Skill '{name}' deleted.",
    }


def _write_file(
    name: str,
    file_path: str,
    file_content: str,
    tenant: Any,
) -> dict[str, Any]:
    """Add or overwrite a supporting file within a skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    err = _validate_content_size(file_content, label=file_path)
    if err:
        return {"success": False, "error": err}

    content_bytes = len(file_content.encode("utf-8"))
    if content_bytes > MAX_SKILL_FILE_BYTES:
        return {
            "success": False,
            "error": (
                f"File content is {content_bytes:,} bytes "
                f"(limit: {MAX_SKILL_FILE_BYTES:,} bytes)."
            ),
        }

    existing = _find_skill(name, tenant)
    if not existing:
        return {
            "success": False,
            "error": f"Skill '{name}' not found. Create it first.",
        }

    target = existing["path"] / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, file_content)

    return {
        "success": True,
        "message": f"File '{file_path}' written to skill '{name}'.",
        "path": str(target),
    }


def _remove_file(
    name: str,
    file_path: str,
    tenant: Any,
) -> dict[str, Any]:
    """Remove a supporting file from a skill directory."""
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    existing = _find_skill(name, tenant)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]
    target = skill_dir / file_path
    if not target.exists():
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
        }

    target.unlink()

    # Clean up empty subdirectory.
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"File '{file_path}' removed from skill '{name}'.",
    }
