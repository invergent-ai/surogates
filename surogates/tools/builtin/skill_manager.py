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
import shutil
import tempfile
from pathlib import Path
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared validation (canonical source: skill_validation.py)
# ---------------------------------------------------------------------------

from surogates.tools.builtin.skill_validation import (
    ALLOWED_SUBDIRS,
    MAX_NAME_LENGTH,
    MAX_SKILL_CONTENT_CHARS,
    MAX_SKILL_FILE_BYTES,
    VALID_NAME_RE,
    validate_category as _validate_category,
    validate_content_size as _validate_content_size,
    validate_file_path as _validate_file_path,
    validate_frontmatter as _validate_frontmatter,
    validate_name as _validate_name,
)

SCHEMA = ToolSchema(
    name="skill_manage",
    description=(
        "Manage skills (create, update, delete). Skills are your procedural "
        "memory — reusable approaches for recurring task types. "
        "New skills go to the user's skills directory; existing skills can be modified wherever they live.\n\n"
        "Actions: create (full SKILL.md + optional category), "
        "patch (old_string/new_string — preferred for fixes), "
        "edit (full SKILL.md rewrite — major overhauls only), "
        "delete, write_file, remove_file.\n\n"
        "Create when: complex task succeeded (5+ calls), errors overcome, "
        "user-corrected approach worked, non-trivial workflow discovered, "
        "or user asks you to remember a procedure.\n"
        "Update when: instructions stale/wrong, OS-specific failures, "
        "missing steps or pitfalls found during use. "
        "If you used a skill and hit issues not covered by it, patch it immediately.\n\n"
        "After difficult/iterative tasks, offer to save as a skill. "
        "Skip for simple one-offs. Confirm with user before creating/deleting.\n\n"
        "Good skills: trigger conditions, numbered steps with exact commands, "
        "pitfalls section, verification steps. Use skill_view() to see format examples."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": (
                    "Skill name (lowercase, hyphens/underscores, max 64 chars). "
                    "Must match an existing skill for patch/edit/delete/write_file/remove_file."
                ),
            },
            "content": {
                "type": "string",
                "description": (
                    "Full SKILL.md content (YAML frontmatter + markdown body). "
                    "Required for 'create' and 'edit'. For 'edit', read the skill "
                    "first with skill_view() and provide the complete updated text."
                ),
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Text to find in the file (required for 'patch'). Must be unique "
                    "unless replace_all=true. Include enough surrounding context to "
                    "ensure uniqueness."
                ),
            },
            "new_string": {
                "type": "string",
                "description": (
                    "Replacement text (required for 'patch'). Can be empty string "
                    "to delete the matched text."
                ),
            },
            "replace_all": {
                "type": "boolean",
                "description": "For 'patch': replace all occurrences instead of requiring a unique match (default: false).",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category/domain for organizing the skill (e.g., 'devops', "
                    "'data-science', 'mlops'). Creates a subdirectory grouping. "
                    "Only used with 'create'."
                ),
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Path to a supporting file within the skill directory. "
                    "For 'write_file'/'remove_file': required, must be under references/, "
                    "templates/, scripts/, or assets/. "
                    "For 'patch': optional, defaults to SKILL.md if omitted."
                ),
            },
            "file_content": {
                "type": "string",
                "description": "Content for the file. Required for 'write_file'.",
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
    # API-mediated mode: delegate all CRUD to the API server.
    api_client = kwargs.get("api_client")
    if api_client is not None:
        return await _dispatch_via_api(api_client, arguments)

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
                "error": "content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).",
            })
        category = arguments.get("category")
        result = _create_skill(name, content, tenant, category)

    elif action == "edit":
        content = arguments.get("content")
        if not content:
            return json.dumps({
                "success": False,
                "error": "content is required for 'edit'. Provide the full updated SKILL.md text.",
            })
        result = _edit_skill(name, content, tenant)

    elif action == "patch":
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not old_string:
            return json.dumps({
                "success": False,
                "error": "old_string is required for 'patch'. Provide the text to find.",
            })
        if new_string is None:
            return json.dumps({
                "success": False,
                "error": "new_string is required for 'patch'. Use empty string to delete matched text.",
            })
        file_path = arguments.get("file_path")
        replace_all = arguments.get("replace_all", False)
        result = _patch_skill(name, old_string, new_string, tenant, file_path, replace_all)

    elif action == "delete":
        result = _delete_skill(name, tenant)

    elif action == "write_file":
        file_path = arguments.get("file_path")
        file_content = arguments.get("file_content")
        if not file_path:
            return json.dumps({
                "success": False,
                "error": "file_path is required for 'write_file'. Example: 'references/api-guide.md'",
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


# Validation functions are imported from skill_validation.py above.
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
    result["hint"] = (
        "To add reference files, templates, or scripts, use "
        "skill_manage(action='write_file', name='{}', file_path='references/example.md', file_content='...')".format(name)
    )
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
            "error": f"Skill '{name}' not found. Use skills_list() to see available skills.",
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
    file_path: str | None = None,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Targeted find-and-replace within a skill file.

    Defaults to SKILL.md.  Use *file_path* to patch a supporting file instead.
    Requires a unique match unless *replace_all* is ``True``.
    """
    existing = _find_skill(name, tenant)
    if not existing:
        return {"success": False, "error": f"Skill '{name}' not found."}

    skill_dir = existing["path"]

    if file_path:
        # Patching a supporting file
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target = skill_dir / file_path
    else:
        # Patching SKILL.md
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {
            "success": False,
            "error": f"File not found: {target.relative_to(skill_dir)}",
        }

    content = target.read_text(encoding="utf-8")

    count = content.count(old_string)
    if count == 0:
        preview = content[:500] + ("..." if len(content) > 500 else "")
        return {
            "success": False,
            "error": "old_string not found in file.",
            "file_preview": preview,
        }

    if count > 1 and not replace_all:
        return {
            "success": False,
            "error": (
                f"old_string matches {count} locations. "
                "Include more surrounding context for a unique match, "
                "or set replace_all=true to replace all occurrences."
            ),
        }

    new_content = content.replace(old_string, new_string)

    # Check size limit on the result
    target_label = "SKILL.md" if not file_path else file_path
    err = _validate_content_size(new_content, label=target_label)
    if err:
        return {"success": False, "error": err}

    # If patching SKILL.md, validate frontmatter is still intact
    if not file_path:
        err = _validate_frontmatter(new_content)
        if err:
            return {
                "success": False,
                "error": f"Patch would break SKILL.md structure: {err}",
            }

    _atomic_write(target, new_content)

    return {
        "success": True,
        "message": (
            f"Patched {'SKILL.md' if not file_path else file_path} in skill '{name}' "
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
            "error": f"Skill '{name}' not found. Create it first with action='create'.",
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
        # List what's actually there for the model to see
        available: list[str] = []
        for subdir in ALLOWED_SUBDIRS:
            d = skill_dir / subdir
            if d.exists():
                for f in d.rglob("*"):
                    if f.is_file():
                        available.append(str(f.relative_to(skill_dir)))
        return {
            "success": False,
            "error": f"File '{file_path}' not found in skill '{name}'.",
            "available_files": available if available else None,
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


# ---------------------------------------------------------------------------
# API-mediated dispatch (when api_client is available)
# ---------------------------------------------------------------------------


async def _dispatch_via_api(api_client: Any, arguments: dict[str, Any]) -> str:
    """Route skill_manage actions through the HarnessAPIClient."""
    action = arguments.get("action", "")
    name = arguments.get("name", "")

    if action == "create":
        content = arguments.get("content", "")
        category = arguments.get("category")
        return await api_client.create_skill(name, content, category)

    if action == "edit":
        content = arguments.get("content", "")
        return await api_client.edit_skill(name, content)

    if action == "patch":
        old_string = arguments.get("old_string", "")
        new_string = arguments.get("new_string", "")
        file_path = arguments.get("file_path")
        replace_all = arguments.get("replace_all", False)
        return await api_client.patch_skill(name, old_string, new_string, file_path, replace_all)

    if action == "delete":
        return await api_client.delete_skill(name)

    if action == "write_file":
        file_path = arguments.get("file_path", "")
        file_content = arguments.get("file_content", "")
        return await api_client.write_skill_file(name, file_path, file_content)

    if action == "remove_file":
        file_path = arguments.get("file_path", "")
        return await api_client.remove_skill_file(name, file_path)

    return json.dumps({
        "success": False,
        "error": f"Unknown action '{action}'.",
    })
