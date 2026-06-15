"""Builtin skills listing and viewing tools.

Progressive disclosure architecture:
- skills_list: List skills with metadata (tier 1 -- name, description, category)
- skill_view: Load full skill content + supporting files on demand (tier 2-3)
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Anthropic-recommended limits for progressive disclosure efficiency
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

_EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))


def _estimate_tokens(content: str) -> int:
    """Rough token estimate (4 chars per token average)."""
    return len(content) // 4


def _parse_tags(tags_value: Any) -> list[str]:
    """Parse tags from frontmatter value.

    Handles:
    - Already-parsed list (from yaml.safe_load): [tag1, tag2]
    - String with brackets: "[tag1, tag2]"
    - Comma-separated string: "tag1, tag2"
    """
    if not tags_value:
        return []

    # yaml.safe_load already returns a list for [tag1, tag2]
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]

    # String fallback -- handle bracket-wrapped or comma-separated
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]

    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

SKILLS_LIST_SCHEMA = ToolSchema(
    name="skills_list",
    description=(
        "List available skills (name + description). Use skill_view(name) "
        "to load full content. Entries with type: expert are specialist "
        "models; consult active experts via consult_expert(expert, task) "
        "rather than skill_view."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional category filter to narrow results",
            }
        },
        "required": [],
    },
)

SKILL_VIEW_SCHEMA = ToolSchema(
    name="skill_view",
    description=(
        "Skills allow for loading information about specific tasks and workflows, "
        "as well as scripts and templates. Load a skill's full content or access "
        "its linked files (references, templates, scripts). First call returns "
        "SKILL.md content plus a 'linked_files' dict showing available "
        "references/templates/scripts.\n\n"
        "When a skill has supporting files, its entire tree is automatically "
        "staged into the sandbox workspace and the response includes a "
        "'staged_at' absolute path. Relative paths in SKILL.md (e.g. "
        "'scripts/build.py', 'assets/template.pptx') resolve against that "
        "directory — run scripts and read assets from there via the "
        "terminal/file tools. You do NOT need to follow up with "
        "skill_view(..., file_path=...) just to ferry bytes into the sandbox."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name (use skills_list to see available skills)",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "OPTIONAL: Path to a linked file within the skill "
                    "(e.g., 'references/api.md'). Prefer reading staged files "
                    "directly from 'staged_at'; use file_path only when you "
                    "want the text inline in your tool result."
                ),
            },
        },
        "required": ["name"],
    },
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    """Register the skills_list and skill_view tools."""
    registry.register(
        name="skills_list",
        schema=SKILLS_LIST_SCHEMA,
        handler=_skills_list_handler,
        toolset="skills",
    )
    registry.register(
        name="skill_view",
        schema=SKILL_VIEW_SCHEMA,
        handler=_skill_view_handler,
        toolset="skills",
    )


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _settings_from_kwargs(kwargs: dict[str, Any]) -> Any | None:
    settings = kwargs.get("settings")
    if settings is not None:
        return settings
    try:
        from surogates.config import load_settings

        return load_settings()
    except Exception:
        logger.debug("Failed to load settings for skill directory lookup", exc_info=True)
        return None


def _session_skill_overrides(kwargs: dict[str, Any]) -> dict[str, dict] | None:
    """Return the session-scoped skill overrides from dispatch kwargs.

    Resolved from an explicit ``skill_overrides`` kwarg or, more commonly,
    from ``session_config['skill_overrides']`` (set at prompt-submission
    time).  Returns ``None`` when the worker kill switch
    (``WorkerSettings.skill_overrides_enabled``) is off so the resolver
    ignores them everywhere uniformly.
    """
    session_config = kwargs.get("session_config") or {}
    overrides = kwargs.get("skill_overrides") or session_config.get("skill_overrides")
    if not overrides:
        return None
    settings = _settings_from_kwargs(kwargs)
    worker_settings = getattr(settings, "worker", settings)
    if not getattr(worker_settings, "skill_overrides_enabled", True):
        return None
    return overrides


def _active_skill_override(kwargs: dict[str, Any], name: str) -> dict | None:
    """Return the active override entry for *name*, or ``None``.

    Used by the worker-local ``skill_view`` path so a disk-backed skill's
    re-read of ``SKILL.md`` is replaced by the candidate body while its
    supporting files keep resolving from the original on-disk tree.
    """
    overrides = _session_skill_overrides(kwargs)
    if not overrides:
        return None
    ov = overrides.get(name)
    if ov and ov.get("content"):
        return ov
    return None


async def _load_all_skills(tenant: Any, **kwargs: Any) -> list:
    """Load skills from all layers, using DB when a session factory is available."""
    from surogates.tools.loader import ResourceLoader

    loader = ResourceLoader.from_settings(_settings_from_kwargs(kwargs))
    overrides = _session_skill_overrides(kwargs)
    session_factory = kwargs.get("session_factory")
    if session_factory is not None:
        async with session_factory() as db_session:
            return await loader.load_skills(
                tenant, db_session=db_session, overrides=overrides,
            )
    return await loader.load_skills(tenant, overrides=overrides)


# ---------------------------------------------------------------------------
# skills_list handler
# ---------------------------------------------------------------------------


async def _skills_list_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """List all available skills (progressive disclosure tier 1 -- minimal metadata).

    Returns only name + description + category to minimise token usage.
    Use skill_view() to load full content, tags, related files, etc.
    """
    # API-mediated mode: delegate to the API server.
    api_client = kwargs.get("api_client")
    if api_client is not None:
        category = arguments.get("category")
        return await api_client.list_skills(category)

    tenant = kwargs.get("tenant")
    if tenant is None:
        return json.dumps({"error": "No tenant context available"})

    skills = await _load_all_skills(**kwargs)

    category_filter = arguments.get("category")

    skill_list: list[dict[str, Any]] = []
    for s in skills:
        if category_filter and s.category != category_filter:
            continue
        # Inactive experts (draft / collecting / retired) are hidden
        # from this catalog.  The slash dispatcher would otherwise fall
        # through to ``skill_view`` and inline the expert's system
        # prompt as if it were a regular skill body — confusing UX.
        # ``# Available Experts`` in the system prompt and the
        # ``consult_expert`` tool already filter to active-only via
        # ``get_active_experts``; this keeps the catalog tool aligned.
        if getattr(s, "is_expert", False) and not getattr(
            s, "is_active_expert", False,
        ):
            continue
        entry: dict[str, Any] = {
            "name": s.name,
            "description": s.description,
            "category": s.category,
            "type": getattr(s, "type", "skill"),
        }
        trigger = getattr(s, "trigger", None)
        if trigger:
            entry["trigger"] = trigger
        if getattr(s, "is_expert", False):
            entry["expert_status"] = getattr(s, "expert_status", None)
            entry["expert_model"] = getattr(s, "expert_model", None)
            entry["expert_endpoint"] = getattr(s, "expert_endpoint", None)
        skill_list.append(entry)

    # Sort by category then name
    skill_list.sort(key=lambda s: (s.get("category") or "", s["name"]))

    # Extract unique categories
    categories = sorted(
        set(s.get("category") for s in skill_list if s.get("category"))
    )

    return json.dumps(
        {
            "success": True,
            "skills": skill_list,
            "categories": categories,
            "count": len(skill_list),
            "hint": "Use skill_view(name) to see full content, tags, and linked files",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# skill_view handler
# ---------------------------------------------------------------------------


def _build_skill_response(
    *,
    name: str,
    description: str,
    tags: list[str],
    related_skills: list[str],
    content: str,
    linked_files: dict[str, list[str]] | None,
    compatibility: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Build the JSON-encoded response shape shared by both view branches.

    The disk-backed branch passes ``compatibility`` / ``metadata`` parsed
    from on-disk SKILL.md frontmatter; the DB-backed branch leaves them
    ``None`` since the loader has already stripped frontmatter from the
    body it stored on ``SkillDef.content``.
    """
    result: dict[str, Any] = {
        "success": True,
        "name": name,
        "description": description,
        "tags": tags,
        "related_skills": related_skills,
        "content": content,
        "linked_files": linked_files if linked_files else None,
        "usage_hint": (
            "To view linked files, call skill_view(name, file_path) where "
            "file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
        )
        if linked_files
        else None,
        "token_estimate": _estimate_tokens(content),
    }
    if compatibility:
        result["compatibility"] = compatibility
    if isinstance(metadata, dict):
        result["metadata"] = metadata
    return json.dumps(result, ensure_ascii=False)


async def _skill_view_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """View the content of a skill or a specific file within a skill directory.

    Progressive disclosure tier 2-3:
    - Tier 2: Full SKILL.md content + linked_files listing
    - Tier 3: Specific linked file content loaded on demand via file_path
    """
    name = arguments.get("name", "")
    file_path = arguments.get("file_path")

    # API-mediated mode: delegate to the API server.
    api_client = kwargs.get("api_client")
    if api_client is not None:
        return await api_client.view_skill(name, file_path)

    tenant = kwargs.get("tenant")
    if tenant is None:
        return json.dumps({"error": "No tenant context available"})

    if not name:
        return json.dumps(
            {"success": False, "error": "Skill name is required."},
            ensure_ascii=False,
        )

    skills = await _load_all_skills(**kwargs)

    # Find the requested skill by name
    matching_skill = None
    for s in skills:
        if s.name == name:
            matching_skill = s
            break

    if matching_skill is None:
        available = [s.name for s in skills[:20]]
        return json.dumps(
            {
                "success": False,
                "error": f"Skill '{name}' not found.",
                "available_skills": available,
                "hint": "Use skills_list to see all available skills",
            },
            ensure_ascii=False,
        )

    # --- DB-backed branch ---------------------------------------------------
    # The loader (``ResourceLoader.load_skills``) gives DB layers precedence
    # over filesystem layers, matching the API-mediated ``view_skill`` route.
    # Branch on source before resolving disk dirs so anonymous website
    # sessions (no ``api_client``) see the same skill body as authenticated
    # ones.  DB-stored skills carry no linked files in this schema.
    from surogates.tools.loader import SKILL_SOURCE_ORG_DB, SKILL_SOURCE_USER_DB

    if matching_skill.source in {SKILL_SOURCE_ORG_DB, SKILL_SOURCE_USER_DB}:
        if not (matching_skill.content or "").strip():
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' has no body.",
                },
                ensure_ascii=False,
            )
        if file_path:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Skill '{name}' is DB-backed and exposes no "
                        "linked files."
                    ),
                    "hint": (
                        "Call skill_view(name) without file_path to read "
                        "the skill body."
                    ),
                },
                ensure_ascii=False,
            )
        return _build_skill_response(
            name=matching_skill.name,
            description=matching_skill.description or "",
            tags=list(matching_skill.tags or []),
            related_skills=[],
            content=matching_skill.content,
            linked_files=None,
        )

    # Resolve the skill directory on disk
    skill_dir = _resolve_skill_dir(
        name,
        tenant,
        settings=_settings_from_kwargs(kwargs),
    )
    if skill_dir is None:
        return json.dumps(
            {
                "success": False,
                "error": f"Skill '{name}' directory not found on disk.",
            },
            ensure_ascii=False,
        )

    skill_md = skill_dir / "SKILL.md"

    # If a specific file path is requested, read that file
    if file_path:
        # Security: Prevent path traversal attacks
        normalized_path = Path(file_path)
        if ".." in normalized_path.parts:
            return json.dumps(
                {
                    "success": False,
                    "error": "Path traversal ('..') is not allowed.",
                    "hint": "Use a relative path within the skill directory",
                },
                ensure_ascii=False,
            )

        target_file = skill_dir / file_path

        # Security: Verify resolved path is still within skill directory
        try:
            resolved = target_file.resolve()
            skill_dir_resolved = skill_dir.resolve()
            if not resolved.is_relative_to(skill_dir_resolved):
                return json.dumps(
                    {
                        "success": False,
                        "error": "Path escapes skill directory boundary.",
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )
        except (OSError, ValueError):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid file path: '{file_path}'",
                    "hint": "Use a valid relative path within the skill directory",
                },
                ensure_ascii=False,
            )

        if not target_file.exists():
            # List available files in the skill directory, organised by type
            available_files: dict[str, list[str]] = {
                "references": [],
                "templates": [],
                "assets": [],
                "scripts": [],
                "other": [],
            }

            for f in skill_dir.rglob("*"):
                if f.is_file() and f.name != "SKILL.md":
                    rel = str(f.relative_to(skill_dir))
                    if rel.startswith("references/"):
                        available_files["references"].append(rel)
                    elif rel.startswith("templates/"):
                        available_files["templates"].append(rel)
                    elif rel.startswith("assets/"):
                        available_files["assets"].append(rel)
                    elif rel.startswith("scripts/"):
                        available_files["scripts"].append(rel)
                    elif f.suffix in [
                        ".md", ".py", ".yaml", ".yml", ".json", ".tex", ".sh",
                    ]:
                        available_files["other"].append(rel)

            # Remove empty categories
            available_files = {k: v for k, v in available_files.items() if v}

            return json.dumps(
                {
                    "success": False,
                    "error": f"File '{file_path}' not found in skill '{name}'.",
                    "available_files": available_files,
                    "hint": "Use one of the available file paths listed above",
                },
                ensure_ascii=False,
            )

        # Read the file content
        try:
            content = target_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary file -- return info about it instead
            return json.dumps(
                {
                    "success": True,
                    "name": name,
                    "file": file_path,
                    "content": f"[Binary file: {target_file.name}, size: {target_file.stat().st_size} bytes]",
                    "is_binary": True,
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "success": True,
                "name": name,
                "file": file_path,
                "content": content,
                "file_type": target_file.suffix,
            },
            ensure_ascii=False,
        )

    # --- Main SKILL.md content (no file_path requested) ---

    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": f"Failed to read skill '{name}': {e}",
            },
            ensure_ascii=False,
        )

    # Parse frontmatter for metadata
    frontmatter = _parse_skill_frontmatter_dict(content)

    # Get reference, template, asset, and script files
    reference_files: list[str] = []
    template_files: list[str] = []
    asset_files: list[str] = []
    script_files: list[str] = []

    references_dir = skill_dir / "references"
    if references_dir.exists():
        reference_files = [
            str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")
        ]

    templates_dir = skill_dir / "templates"
    if templates_dir.exists():
        for ext in ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"]:
            template_files.extend(
                [str(f.relative_to(skill_dir)) for f in templates_dir.rglob(ext)]
            )

    assets_dir = skill_dir / "assets"
    if assets_dir.exists():
        for f in assets_dir.rglob("*"):
            if f.is_file():
                asset_files.append(str(f.relative_to(skill_dir)))

    scripts_dir = skill_dir / "scripts"
    if scripts_dir.exists():
        for ext in ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"]:
            script_files.extend(
                [str(f.relative_to(skill_dir)) for f in scripts_dir.glob(ext)]
            )

    # Read tags/related_skills: check metadata.hermes.* first, fall back to top-level
    hermes_meta: dict[str, Any] = {}
    metadata = frontmatter.get("metadata")
    if isinstance(metadata, dict):
        hermes_meta = metadata.get("hermes", {}) or {}

    tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
    related_skills = _parse_tags(
        hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
    )

    # Build linked files structure for clear discovery
    linked_files: dict[str, list[str]] = {}
    if reference_files:
        linked_files["references"] = reference_files
    if template_files:
        linked_files["templates"] = template_files
    if asset_files:
        linked_files["assets"] = asset_files
    if script_files:
        linked_files["scripts"] = script_files

    # Session-scoped override: serve the candidate body for THIS session
    # while keeping the original skill's supporting-file listing.  Mirrors
    # the shared-runtime API path (``view_skill``), which serves the patched
    # ``SkillDef.content``/``description`` and stages original files.
    override = _active_skill_override(kwargs, name)
    if override is not None:
        response_content = override["content"]
        response_description = (
            matching_skill.description or frontmatter.get("description", "")
        )
    else:
        response_content = content
        response_description = frontmatter.get("description", "")

    return _build_skill_response(
        name=frontmatter.get("name", name),
        description=response_description,
        tags=tags,
        related_skills=related_skills,
        content=response_content,
        linked_files=linked_files if linked_files else None,
        compatibility=frontmatter.get("compatibility"),
        metadata=metadata if isinstance(metadata, dict) else None,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_skill_dir(
    name: str,
    tenant: Any,
    *,
    settings: Any | None = None,
) -> Path | None:
    """Find the on-disk directory for a skill by name.

    Searches user and org-shared workspace layers in precedence order.
    Platform skills live in the per-agent Surogate Hub bundle and are
    resolved via the bundle accessor, not the workspace filesystem.
    """
    asset_root = Path(tenant.asset_root)
    org_id = str(tenant.org_id)
    user_id = str(tenant.user_id)

    search_dirs = [
        asset_root / org_id / "users" / user_id / "skills",
        asset_root / org_id / "shared" / "skills",
    ]

    for skills_dir in search_dirs:
        if not skills_dir.is_dir():
            continue
        # Direct match: skills_dir/name/SKILL.md
        candidate = skills_dir / name / "SKILL.md"
        if candidate.is_file():
            return candidate.parent
        # Category match: skills_dir/*/name/SKILL.md
        for subdir in skills_dir.iterdir():
            if subdir.is_dir() and subdir.name not in _EXCLUDED_SKILL_DIRS:
                candidate = subdir / name / "SKILL.md"
                if candidate.is_file():
                    return candidate.parent
    return None


def _parse_skill_frontmatter_dict(content: str) -> dict[str, Any]:
    """Extract YAML frontmatter from skill content as a dict."""
    if not content.strip().startswith("---"):
        return {}

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return {}

    yaml_content = content[3: end_match.start() + 3]
    try:
        import yaml
        parsed = yaml.safe_load(yaml_content)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}
