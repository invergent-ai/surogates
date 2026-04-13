"""Resource loader for skills, tools, and MCP server configurations.

Merges resources from three layers with increasing precedence:

1. **Platform** -- baked into the container image at well-known paths.
2. **Org (shared)** -- stored under the tenant's shared asset root.
3. **User** -- stored under the tenant's per-user asset root.

When the same resource name appears in multiple layers, the higher-
precedence layer wins.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Well-known platform volume paths (overridable via constructor).
PLATFORM_SKILLS_DIR = "/etc/surogates/skills"
PLATFORM_TOOLS_DIR = "/etc/surogates/tools"
PLATFORM_MCP_DIR = "/etc/surogates/mcp"


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))

# Expert status lifecycle constants.
EXPERT_STATUS_DRAFT = "draft"
EXPERT_STATUS_COLLECTING = "collecting"
EXPERT_STATUS_ACTIVE = "active"
EXPERT_STATUS_RETIRED = "retired"

# Mapping from SKILL.md frontmatter keys to SkillDef field names.
# Keys not in this map are ignored.
_EXPERT_FRONTMATTER_MAP: dict[str, str] = {
    "base_model": "expert_model",
    "endpoint": "expert_endpoint",
    "adapter": "expert_adapter",
    "max_iterations": "expert_max_iterations",
    "expert_status": "expert_status",
}


@dataclass(slots=True)
class SkillDef:
    """A loaded skill definition.

    When ``type`` is ``"expert"``, the skill is backed by a fine-tuned
    small language model (SLM) instead of a prompt template.  The base
    LLM delegates to it via the ``consult_expert`` tool.
    """

    name: str
    description: str
    content: str  # The SKILL.md body (everything after the frontmatter)
    source: str  # "platform", "org", "user"
    type: str = "skill"  # "skill" (prompt-based) or "expert" (SLM-backed)
    category: str | None = None  # subdirectory grouping
    tags: list[str] | None = None  # metadata tags
    # Conditional activation fields (parsed from frontmatter).
    platforms: list[str] | None = None  # e.g. ["linux", "macos"]
    fallback_for_tools: list[str] | None = None  # show only when these tools are unavailable
    requires_tools: list[str] | None = None  # show only when these tools ARE available
    trigger: str | None = None  # trigger description
    # Expert-specific fields (None/default for regular skills).
    expert_model: str | None = None  # base model name (e.g. "qwen2.5-coder-7b")
    expert_endpoint: str | None = None  # OpenAI-compatible inference URL
    expert_adapter: str | None = None  # LoRA adapter path in tenant storage
    expert_max_iterations: int = 10  # iteration budget for expert mini-loop
    expert_status: str = EXPERT_STATUS_DRAFT  # draft → collecting → active → retired
    expert_tools: list[str] | None = None  # tools the expert can use in its mini-loop

    @property
    def is_expert(self) -> bool:
        """Return ``True`` if this skill is backed by a fine-tuned model."""
        return self.type == "expert"

    @property
    def is_active_expert(self) -> bool:
        """Return ``True`` if this is an active, usable expert."""
        return self.is_expert and self.expert_status == EXPERT_STATUS_ACTIVE


@dataclass(slots=True)
class MCPServerDef:
    """Configuration for a single MCP server."""

    name: str
    transport: str  # "stdio" or "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 120


# ------------------------------------------------------------------
# Loader
# ------------------------------------------------------------------


class ResourceLoader:
    """Loads skills and MCP server configs from platform volumes and
    tenant asset roots.

    Parameters
    ----------
    platform_skills_dir:
        Path to the platform-level skills directory.
    platform_mcp_dir:
        Path to the platform-level MCP config directory.
    """

    def __init__(
        self,
        platform_skills_dir: str = PLATFORM_SKILLS_DIR,
        platform_mcp_dir: str = PLATFORM_MCP_DIR,
    ) -> None:
        self._platform_skills_dir = platform_skills_dir
        self._platform_mcp_dir = platform_mcp_dir

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    def load_skills(self, tenant: Any) -> list[SkillDef]:
        """Merge skills from platform + org shared + user layers.

        Parameters
        ----------
        tenant:
            A :class:`~surogates.tenant.context.TenantContext` instance.

        Returns
        -------
        list[SkillDef]
            Deduplicated by name.  User layer wins over org, which wins
            over platform.
        """
        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        user_id = str(tenant.user_id)

        org_skills_dir = str(asset_root / org_id / "shared" / "skills")
        user_skills_dir = str(
            asset_root / org_id / "users" / user_id / "skills"
        )

        platform = self._load_skills_from_dir(self._platform_skills_dir, "platform")
        org = self._load_skills_from_dir(org_skills_dir, "org")
        user = self._load_skills_from_dir(user_skills_dir, "user")

        return self._merge(platform, org, user)

    # ------------------------------------------------------------------
    # Conditional skill filtering
    # ------------------------------------------------------------------

    @staticmethod
    def filter_skills(
        skills: list[SkillDef],
        available_tools: set[str],
    ) -> list[SkillDef]:
        """Filter skills based on conditional activation rules.

        - If ``fallback_for_tools`` is set and ALL those tools are
          available, skip the skill (it is only a fallback).
        - If ``requires_tools`` is set and ANY are missing, skip.
        """
        filtered: list[SkillDef] = []
        for skill in skills:
            if skill.fallback_for_tools and all(
                t in available_tools for t in skill.fallback_for_tools
            ):
                continue
            if skill.requires_tools and not all(
                t in available_tools for t in skill.requires_tools
            ):
                continue
            filtered.append(skill)
        return filtered

    # ------------------------------------------------------------------
    # MCP servers
    # ------------------------------------------------------------------

    def load_mcp_servers(self, tenant: Any) -> list[MCPServerDef]:
        """Merge MCP server configs from platform + org shared + user layers.

        Parameters
        ----------
        tenant:
            A :class:`~surogates.tenant.context.TenantContext` instance.

        Returns
        -------
        list[MCPServerDef]
            Deduplicated by name.  User layer wins over org, which wins
            over platform.
        """
        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        user_id = str(tenant.user_id)

        org_mcp_dir = str(asset_root / org_id / "shared" / "mcp")
        user_mcp_dir = str(
            asset_root / org_id / "users" / user_id / "mcp"
        )

        platform = self._load_mcp_from_dir(self._platform_mcp_dir)
        org = self._load_mcp_from_dir(org_mcp_dir)
        user = self._load_mcp_from_dir(user_mcp_dir)

        return self._merge(platform, org, user)

    # ------------------------------------------------------------------
    # Skills parsing
    # ------------------------------------------------------------------

    def _load_skills_from_dir(self, path: str, source: str) -> list[SkillDef]:
        """Load skills from *path*, supporting both directory-based and flat layouts.

        Directory-based layout (preferred)::

            skills/
            ├── my-skill/
            │   ├── SKILL.md           # Main instructions (required)
            │   ├── references/
            │   └── templates/
            └── category/
                └── another-skill/
                    └── SKILL.md

        Flat layout (legacy)::

            skills/
            ├── my-skill.md
            └── another-skill.md

        If no frontmatter is present the directory name (or filename minus
        extension) is used as the skill name.
        """
        directory = Path(path)
        if not directory.is_dir():
            return []

        skills: list[SkillDef] = []
        seen_names: set[str] = set()

        # Walk for SKILL.md files (directory-based layout).
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
            if "SKILL.md" in files:
                skill_md = Path(root) / "SKILL.md"
                try:
                    text = skill_md.read_text(encoding="utf-8")
                    parsed = _parse_skill_frontmatter(text, skill_md.parent.name)
                    name = parsed["name"]
                    if name in seen_names:
                        continue
                    seen_names.add(name)

                    # Extract category from path structure.
                    category = _get_category_from_path(skill_md, directory)

                    skills.append(
                        _build_skill_def(parsed, source, category),
                    )
                except Exception:
                    logger.exception("Failed to load skill from %s", skill_md)

        # Flat .md files at the top level (legacy layout).
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(".md"):
                continue
            if entry.name == "SKILL.md":
                continue
            try:
                text = entry.read_text(encoding="utf-8")
                parsed = _parse_skill_frontmatter(text, entry.stem)
                name = parsed["name"]
                if name in seen_names:
                    continue
                seen_names.add(name)
                skills.append(
                    _build_skill_def(parsed, source),
                )
            except Exception:
                logger.exception("Failed to load skill from %s", entry)

        return skills

    # ------------------------------------------------------------------
    # MCP config parsing
    # ------------------------------------------------------------------

    def _load_mcp_from_dir(self, path: str) -> list[MCPServerDef]:
        """Load MCP server definitions from *path*.

        Supports two layouts:

        * A single ``servers.json`` (or ``servers.yaml`` / ``servers.yml``)
          containing a mapping of server name to config.
        * Individual ``.json`` / ``.yaml`` / ``.yml`` files, each
          containing a single server config with a ``name`` key.
        """
        directory = Path(path)
        if not directory.is_dir():
            return []

        servers: list[MCPServerDef] = []

        # Try consolidated file first.
        for consolidated_name in ("servers.json", "servers.yaml", "servers.yml"):
            consolidated = directory / consolidated_name
            if consolidated.is_file():
                try:
                    data = _load_data_file(consolidated)
                    if isinstance(data, dict):
                        for server_name, server_cfg in data.items():
                            if isinstance(server_cfg, dict):
                                servers.append(
                                    _parse_mcp_server(server_name, server_cfg)
                                )
                    return servers
                except Exception:
                    logger.exception(
                        "Failed to load consolidated MCP config %s",
                        consolidated,
                    )
                    return []

        # Fall back to individual files.
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix not in (".json", ".yaml", ".yml"):
                continue
            try:
                data = _load_data_file(entry)
                if isinstance(data, dict):
                    server_name = data.get("name", entry.stem)
                    servers.append(_parse_mcp_server(server_name, data))
            except Exception:
                logger.exception("Failed to load MCP config from %s", entry)

        return servers

    # ------------------------------------------------------------------
    # Merge logic
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(
        platform: list[SkillDef | MCPServerDef],
        org: list[SkillDef | MCPServerDef],
        user: list[SkillDef | MCPServerDef],
    ) -> list[Any]:
        """Layer precedence: user > org > platform.

        Items with the same ``name`` in a higher layer replace those in a
        lower layer.
        """
        merged: dict[str, Any] = {}
        for item in platform:
            merged[item.name] = item
        for item in org:
            merged[item.name] = item
        for item in user:
            merged[item.name] = item
        return list(merged.values())


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _build_skill_def(
    parsed: dict[str, Any],
    source: str,
    category: str | None = None,
) -> SkillDef:
    """Construct a :class:`SkillDef` from parsed frontmatter data."""
    max_iter = parsed.get("expert_max_iterations")
    if max_iter is not None:
        try:
            max_iter = int(max_iter)
        except (TypeError, ValueError):
            max_iter = 10
    else:
        max_iter = 10

    return SkillDef(
        name=parsed["name"],
        description=parsed["description"],
        content=parsed["content"],
        source=source,
        type=parsed.get("type", "skill"),
        category=category,
        tags=parsed.get("tags"),
        platforms=parsed.get("platforms"),
        fallback_for_tools=parsed.get("fallback_for_tools"),
        requires_tools=parsed.get("requires_tools"),
        trigger=parsed.get("trigger"),
        expert_model=parsed.get("expert_model"),
        expert_endpoint=parsed.get("expert_endpoint"),
        expert_adapter=parsed.get("expert_adapter"),
        expert_max_iterations=max_iter,
        expert_status=str(parsed.get("expert_status", EXPERT_STATUS_DRAFT)),
        expert_tools=parsed.get("expert_tools"),
    )


def _parse_skill_frontmatter(
    text: str,
    fallback_name: str,
) -> dict[str, Any]:
    """Extract YAML frontmatter and body from a skill file.

    Returns a dict with keys: ``name``, ``description``, ``content``,
    and optional ``platforms``, ``fallback_for_tools``, ``requires_tools``,
    ``trigger``, ``tags``.
    """
    result: dict[str, Any] = {
        "name": fallback_name,
        "description": "",
        "content": text,
    }

    stripped = text.strip()
    if stripped.startswith("---"):
        # Find the closing delimiter.
        end_idx = stripped.find("---", 3)
        if end_idx != -1:
            frontmatter_text = stripped[3:end_idx].strip()
            result["content"] = stripped[end_idx + 3:].strip()

            # Parse the frontmatter as YAML (or simple key: value lines).
            fm = _parse_yaml_or_simple(frontmatter_text)
            result["name"] = fm.get("name", fallback_name)
            result["description"] = fm.get("description", "")

            # Conditional activation fields.
            for key in ("platforms", "fallback_for_tools", "requires_tools"):
                val = fm.get(key)
                if val:
                    if isinstance(val, str):
                        result[key] = [v.strip() for v in val.split(",")]
                    elif isinstance(val, list):
                        result[key] = [str(v) for v in val]

            # Extract metadata.hermes.* fields (agentskills.io convention).
            metadata = fm.get("metadata")
            if isinstance(metadata, dict):
                hermes_meta = metadata.get("hermes")
                if isinstance(hermes_meta, dict):
                    # fallback_for_toolsets, requires_toolsets, fallback_for_tools, requires_tools
                    for cond_key in ("fallback_for_toolsets", "requires_toolsets",
                                     "fallback_for_tools", "requires_tools"):
                        val = hermes_meta.get(cond_key)
                        if val and cond_key not in result:
                            if isinstance(val, str):
                                result[cond_key] = [v.strip() for v in val.split(",")]
                            elif isinstance(val, list):
                                result[cond_key] = [str(v) for v in val]

            # Tags: check metadata.hermes.tags first, fall back to top-level.
            tags = None
            if isinstance(fm.get("metadata"), dict):
                hermes_meta = (fm["metadata"].get("hermes") or {})
                if isinstance(hermes_meta, dict):
                    tags = hermes_meta.get("tags")
            if not tags:
                tags = fm.get("tags")
            if tags:
                if isinstance(tags, str):
                    result["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
                elif isinstance(tags, list):
                    result["tags"] = [str(t).strip() for t in tags if t]

            trigger = fm.get("trigger")
            if trigger:
                result["trigger"] = str(trigger)

            # Skill type: "skill" (default) or "expert" (SLM-backed).
            skill_type = fm.get("type")
            if skill_type and str(skill_type).lower() in ("expert", "skill"):
                result["type"] = str(skill_type).lower()

            # Expert-specific fields (only meaningful when type=expert).
            for fm_key, field_name in _EXPERT_FRONTMATTER_MAP.items():
                val = fm.get(fm_key)
                if val is not None:
                    result[field_name] = val

            # Expert tools list (tools the expert can use in its mini-loop).
            expert_tools = fm.get("tools")
            if expert_tools:
                if isinstance(expert_tools, str):
                    result["expert_tools"] = [t.strip() for t in expert_tools.split(",")]
                elif isinstance(expert_tools, list):
                    result["expert_tools"] = [str(t) for t in expert_tools]

    return result


def _get_category_from_path(skill_md: Path, skills_dir: Path) -> str | None:
    """Extract category from skill path based on directory structure.

    For paths like: skills_dir/mlops/axolotl/SKILL.md -> "mlops"
    """
    try:
        rel_path = skill_md.relative_to(skills_dir)
        parts = rel_path.parts
        # parts = ("category", "skill-name", "SKILL.md") -> category = parts[0]
        if len(parts) >= 3:
            return parts[0]
    except ValueError:
        pass
    return None


def _parse_yaml_or_simple(text: str) -> dict[str, Any]:
    """Parse YAML frontmatter, falling back to a simple ``key: value``
    line parser if PyYAML is unavailable.

    Values that are lists are preserved as lists; scalars are coerced to
    strings.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            result: dict[str, Any] = {}
            for k, v in data.items():
                if isinstance(v, list):
                    result[k] = v
                else:
                    result[k] = str(v)
            return result
        return {}
    except ImportError:
        pass

    # Simple fallback parser for ``key: value`` lines.
    result_simple: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result_simple[key.strip()] = value.strip()
    return result_simple


def _load_data_file(path: Path) -> Any:
    """Load a JSON or YAML file and return the parsed data."""
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return _parse_yaml_data(text)
    return json.loads(text)


def _parse_yaml_data(text: str) -> Any:
    """Parse YAML text, falling back to JSON if PyYAML is unavailable."""
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        logger.debug("PyYAML not available; attempting JSON parse")
        return json.loads(text)


def _parse_mcp_server(name: str, cfg: dict[str, Any]) -> MCPServerDef:
    """Build an :class:`MCPServerDef` from a raw config dict."""
    return MCPServerDef(
        name=name,
        transport=cfg.get("transport", "stdio"),
        command=cfg.get("command"),
        args=cfg.get("args", []),
        url=cfg.get("url"),
        env=cfg.get("env", {}),
        timeout=cfg.get("timeout", 120),
    )


def update_frontmatter_field(content: str, key: str, value: str) -> str:
    """Update or insert a field in the YAML frontmatter of a SKILL.md.

    If *key* already exists in the frontmatter, its value is replaced.
    If not, the key is appended before the closing ``---``.

    Returns the original content unchanged if no frontmatter delimiters
    are found.
    """
    import re

    stripped = content.strip()
    if not stripped.startswith("---"):
        return content

    end_match = re.search(r"\n---\s*(\n|$)", stripped[3:])
    if not end_match:
        return content

    fm_start = 3
    fm_end = end_match.start() + 3
    frontmatter = stripped[fm_start:fm_end]
    after_fm = stripped[fm_end:]

    pattern = re.compile(rf"^({re.escape(key)}\s*:).*$", re.MULTILINE)
    if pattern.search(frontmatter):
        new_fm = pattern.sub(rf"\1 {value}", frontmatter)
    else:
        new_fm = frontmatter.rstrip() + f"\n{key}: {value}"

    return f"---{new_fm}{after_fm}"
