"""Resource loader for skills, tools, and MCP server configurations.

Merges resources from four layers with increasing precedence:

1. **Platform** -- baked into the container image at well-known paths.
2. **User files** -- stored under the tenant's per-user asset root
   (managed by end users via the agent's ``skill_manage`` tool).
3. **Org DB** -- ``skills`` / ``mcp_servers`` table rows where
   ``user_id IS NULL`` (managed by org admin via API).
4. **User DB** -- ``skills`` / ``mcp_servers`` table rows where
   ``user_id`` matches (managed by org admin via API).

Org admin overrides (DB layers) are final -- end users cannot override
them via bucket files.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID


class MCPTransport(str, Enum):
    """Supported MCP transport kinds."""

    STDIO = "stdio"
    HTTP = "http"

logger = logging.getLogger(__name__)

# Well-known platform volume paths (overridable via constructor).
PLATFORM_SKILLS_DIR = "/etc/surogates/skills"
PLATFORM_MCP_DIR = "/etc/surogates/mcp"
PLATFORM_AGENTS_DIR = "/etc/surogates/agents"


# ------------------------------------------------------------------
# Data classes
# ------------------------------------------------------------------


EXCLUDED_SKILL_DIRS = frozenset((".git", ".github", ".hub"))
EXCLUDED_AGENT_DIRS = frozenset((".git", ".github"))

# Skill source layer constants.
SKILL_SOURCE_PLATFORM = "platform"
SKILL_SOURCE_USER = "user"
SKILL_SOURCE_ORG = "org"
SKILL_SOURCE_ORG_DB = "org_db"
SKILL_SOURCE_USER_DB = "user_db"

# Agent source layer constants -- mirror the skill layers.
AGENT_SOURCE_PLATFORM = "platform"
AGENT_SOURCE_USER = "user"
AGENT_SOURCE_ORG = "org"
AGENT_SOURCE_ORG_DB = "org_db"
AGENT_SOURCE_USER_DB = "user_db"

# Recognised AGENT.md frontmatter keys.  Anything outside this set is
# logged as a typo/unknown warning by ``_parse_agent_frontmatter`` so an
# admin who writes ``disallow_tools`` or ``max_iteration`` sees feedback
# instead of silently running an unconstrained agent.
_KNOWN_AGENT_FRONTMATTER_KEYS: frozenset[str] = frozenset({
    "name", "description",
    "tools", "disallowed_tools",
    "model", "max_iterations", "policy_profile",
    "category", "tags", "enabled",
})

# Expert status lifecycle constants.
EXPERT_STATUS_DRAFT = "draft"
EXPERT_STATUS_COLLECTING = "collecting"
EXPERT_STATUS_ACTIVE = "active"
EXPERT_STATUS_RETIRED = "retired"

# Mapping from SKILL.md frontmatter keys to SkillDef field names.
# Keys not in this map are ignored.
_EXPERT_FRONTMATTER_MAP: dict[str, str] = {
    "base_model": "expert_model",
    "model": "expert_model",
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
    source: str  # "platform", "user", "org_db", "user_db"
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
    task_categories: list[str] = field(default_factory=list)  # hard-task routing categories

    @property
    def is_expert(self) -> bool:
        """Return ``True`` if this skill is backed by a fine-tuned model."""
        return self.type == "expert"

    @property
    def is_active_expert(self) -> bool:
        """Return ``True`` if this is an active, usable expert."""
        return self.is_expert and self.expert_status == EXPERT_STATUS_ACTIVE


@dataclass(slots=True)
class AgentDef:
    """A loaded sub-agent type definition.

    A sub-agent type is a declarative bundle of (system prompt, tool
    allowlist/denylist, model override, iteration cap, policy profile).
    When a coordinator session spawns a worker with ``agent_type=<name>``,
    a child ``Session`` is created and these fields are applied to its
    config.  The child inherits skills, MCP servers, experts, tenant
    memory, and workspace from the parent tenant.

    The ``system_prompt`` field is the AGENT.md body (everything after
    the YAML frontmatter).  When the child session wakes, this body
    replaces the default identity section of the system prompt.
    """

    name: str
    description: str
    system_prompt: str  # AGENT.md body (everything after the frontmatter)
    source: str  # "platform", "user", "org", "org_db", "user_db"
    tools: list[str] | None = None  # allowlist of tool names (None = inherit)
    disallowed_tools: list[str] | None = None  # denylist (removed from inherited)
    model: str | None = None  # optional model override for the child session
    max_iterations: int | None = None  # optional cap on the child's iteration budget
    policy_profile: str | None = None  # named governance profile to apply
    enabled: bool = True
    category: str | None = None  # subdirectory grouping
    tags: list[str] | None = None  # metadata tags


@dataclass(slots=True)
class MCPServerDef:
    """Configuration for a single MCP server."""

    name: str
    transport: MCPTransport = MCPTransport.STDIO
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 120


# ------------------------------------------------------------------
# Loader
# ------------------------------------------------------------------


class ResourceLoader:
    """Loads skills, MCP server configs, and sub-agent types from platform
    volumes and tenant asset roots.

    Parameters
    ----------
    platform_skills_dir:
        Path to the platform-level skills directory.
    platform_mcp_dir:
        Path to the platform-level MCP config directory.
    platform_agents_dir:
        Path to the platform-level sub-agent types directory.
    """

    def __init__(
        self,
        platform_skills_dir: str | None = None,
        platform_mcp_dir: str | None = None,
        platform_agents_dir: str | None = None,
    ) -> None:
        # Resolve defaults lazily so runtime monkey-patching of the module
        # constants (used by tests) takes effect.
        self._platform_skills_dir = (
            platform_skills_dir if platform_skills_dir is not None
            else PLATFORM_SKILLS_DIR
        )
        self._platform_mcp_dir = (
            platform_mcp_dir if platform_mcp_dir is not None
            else PLATFORM_MCP_DIR
        )
        self._platform_agents_dir = (
            platform_agents_dir if platform_agents_dir is not None
            else PLATFORM_AGENTS_DIR
        )

    # ------------------------------------------------------------------
    # Platform skill directory resolution
    # ------------------------------------------------------------------

    def resolve_platform_skill_dir(self, name: str) -> Path | None:
        """Return the on-disk directory for a platform skill, or ``None``.

        Searches ``{platform_skills_dir}/{name}/SKILL.md`` and
        ``{platform_skills_dir}/{category}/{name}/SKILL.md`` layouts.
        """
        root = Path(self._platform_skills_dir)
        if not root.is_dir():
            return None
        direct = root / name / "SKILL.md"
        if direct.is_file():
            return direct.parent
        for sub in root.iterdir():
            if not sub.is_dir() or sub.name in EXCLUDED_SKILL_DIRS:
                continue
            candidate = sub / name / "SKILL.md"
            if candidate.is_file():
                return candidate.parent
        return None

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    async def load_skills(
        self,
        tenant: Any,
        db_session: Any | None = None,
    ) -> list[SkillDef]:
        """Merge skills from all four layers.

        Layer precedence (lowest → highest):

        1. Platform filesystem (``/etc/surogates/skills/``)
        2. User bucket files (``tenant-{org}/users/{user}/skills/``)
        3. Org-wide DB rows (``skills`` table, ``user_id IS NULL``)
        4. User-specific DB rows (``skills`` table, ``user_id = ?``)

        Org admin overrides (DB layers) are final.

        Parameters
        ----------
        tenant:
            A :class:`~surogates.tenant.context.TenantContext` instance.
        db_session:
            An optional ``AsyncSession``.  When provided, layers 3 and 4
            are loaded from the database.  When ``None`` the method falls
            back to the legacy 3-layer filesystem merge.
        """
        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        user_id = str(tenant.user_id)

        user_skills_dir = str(
            asset_root / org_id / "users" / user_id / "skills"
        )

        # Layer 1: platform filesystem
        platform = self._load_skills_from_dir(
            self._platform_skills_dir, SKILL_SOURCE_PLATFORM,
        )

        # Layer 2: user bucket files
        user_files = self._load_skills_from_dir(user_skills_dir, SKILL_SOURCE_USER)

        if db_session is not None:
            # Layer 3: org-wide DB entries
            org_db = await self._load_skills_from_db(
                db_session, tenant.org_id, user_id=None, source=SKILL_SOURCE_ORG_DB,
            )
            # Layer 4: user-specific DB entries
            user_db = await self._load_skills_from_db(
                db_session, tenant.org_id, tenant.user_id, source=SKILL_SOURCE_USER_DB,
            )
            return self._merge(platform, user_files, org_db, user_db)

        # Fallback: legacy 3-layer filesystem merge (no DB).
        org_skills_dir = str(asset_root / org_id / "shared" / "skills")
        org_files = self._load_skills_from_dir(org_skills_dir, SKILL_SOURCE_ORG)
        return self._merge(platform, org_files, user_files)

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
    # Sub-agent types
    # ------------------------------------------------------------------

    def resolve_platform_agent_dir(self, name: str) -> Path | None:
        """Return the on-disk directory for a platform sub-agent, or ``None``.

        Searches ``{platform_agents_dir}/{name}/AGENT.md`` and
        ``{platform_agents_dir}/{category}/{name}/AGENT.md`` layouts.
        """
        root = Path(self._platform_agents_dir)
        if not root.is_dir():
            return None
        direct = root / name / "AGENT.md"
        if direct.is_file():
            return direct.parent
        for sub in root.iterdir():
            if not sub.is_dir() or sub.name in EXCLUDED_AGENT_DIRS:
                continue
            candidate = sub / name / "AGENT.md"
            if candidate.is_file():
                return candidate.parent
        return None

    async def load_agents(
        self,
        tenant: Any,
        db_session: Any | None = None,
    ) -> list[AgentDef]:
        """Merge sub-agent types from all four layers.

        Layer precedence (lowest → highest):

        1. Platform filesystem (``/etc/surogates/agents/``)
        2. User bucket files (``tenant-{org}/users/{user}/agents/``)
        3. Org-wide DB rows (``agents`` table, ``user_id IS NULL``)
        4. User-specific DB rows (``agents`` table, ``user_id = ?``)

        Org admin overrides (DB layers) are final -- end users cannot
        override them via bucket files.

        Parameters
        ----------
        tenant:
            A :class:`~surogates.tenant.context.TenantContext` instance.
        db_session:
            An optional ``AsyncSession``.  When provided, layers 3 and 4
            are loaded from the database.  When ``None`` the method
            falls back to the legacy 3-layer filesystem merge.
        """
        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        user_id = str(tenant.user_id)

        user_agents_dir = str(
            asset_root / org_id / "users" / user_id / "agents"
        )

        # Layer 1: platform filesystem
        platform = self._load_agents_from_dir(
            self._platform_agents_dir, AGENT_SOURCE_PLATFORM,
        )

        # Layer 2: user bucket files
        user_files = self._load_agents_from_dir(
            user_agents_dir, AGENT_SOURCE_USER,
        )

        if db_session is not None:
            # Layer 3: org-wide DB entries
            org_db = await self._load_agents_from_db(
                db_session, tenant.org_id, user_id=None,
                source=AGENT_SOURCE_ORG_DB,
            )
            # Layer 4: user-specific DB entries
            user_db = await self._load_agents_from_db(
                db_session, tenant.org_id, tenant.user_id,
                source=AGENT_SOURCE_USER_DB,
            )
            return self._merge(platform, user_files, org_db, user_db)

        # Fallback: legacy 3-layer filesystem merge (no DB).
        org_agents_dir = str(asset_root / org_id / "shared" / "agents")
        org_files = self._load_agents_from_dir(
            org_agents_dir, AGENT_SOURCE_ORG,
        )
        return self._merge(platform, org_files, user_files)

    def load_policy_profile(
        self,
        tenant: Any,
        name: str,
    ) -> dict[str, Any] | None:
        """Load a named policy profile as a raw config dict.

        Profiles are YAML or JSON files stored under
        ``agents/policies/<name>.{yaml,yml,json}`` at the platform and org
        layers.  The profile schema mirrors the top-level governance config:

        * ``allowed_tools: list[str]``  -- narrows the base allowlist
        * ``denied_tools: list[str]``   -- additive denylist
        * ``egress``: see :meth:`GovernanceGate.from_config` -- appended to
          the base egress rules
        * ``enabled: bool``             -- rarely used; overrides base

        Precedence: platform < org.  When a key exists in both layers the
        org value wins (mirrors the ``from_config`` merge semantics).
        Returns ``None`` when no matching profile file exists.
        """
        layers: list[dict[str, Any]] = []

        platform_file = _find_policy_profile_file(
            Path(self._platform_agents_dir) / "policies", name,
        )
        if platform_file is not None:
            try:
                data = _load_data_file(platform_file)
                if isinstance(data, dict):
                    layers.append(data)
            except Exception:
                logger.exception(
                    "Failed to load platform policy profile %s", platform_file,
                )

        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        org_policies_dir = (
            asset_root / org_id / "shared" / "agents" / "policies"
        )
        org_file = _find_policy_profile_file(org_policies_dir, name)
        if org_file is not None:
            try:
                data = _load_data_file(org_file)
                if isinstance(data, dict):
                    layers.append(data)
            except Exception:
                logger.exception(
                    "Failed to load org policy profile %s", org_file,
                )

        if not layers:
            return None

        # Merge: last layer wins for scalar fields; tool lists are unioned.
        merged: dict[str, Any] = {}
        allowed: set[str] = set()
        denied: set[str] = set()
        egress_rules: list[dict[str, Any]] = []
        egress_default: str | None = None
        for layer in layers:
            for key, value in layer.items():
                if key == "allowed_tools" and isinstance(value, list):
                    allowed.update(value)
                elif key == "denied_tools" and isinstance(value, list):
                    denied.update(value)
                elif key == "egress" and isinstance(value, dict):
                    rules = value.get("rules")
                    if isinstance(rules, list):
                        egress_rules.extend(rules)
                    if "default_action" in value:
                        egress_default = value.get("default_action")
                else:
                    merged[key] = value
        if allowed:
            merged["allowed_tools"] = sorted(allowed)
        if denied:
            merged["denied_tools"] = sorted(denied)
        if egress_rules or egress_default:
            egress: dict[str, Any] = {}
            if egress_rules:
                egress["rules"] = egress_rules
            if egress_default:
                egress["default_action"] = egress_default
            merged["egress"] = egress
        return merged

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
    # Sub-agent parsing
    # ------------------------------------------------------------------

    def _load_agents_from_dir(self, path: str, source: str) -> list[AgentDef]:
        """Load sub-agent types from *path*.

        Expected layout::

            agents/
            ├── code-reviewer/
            │   └── AGENT.md
            ├── category/
            │   └── experiment-runner/
            │       └── AGENT.md
            └── db-reader.md          # flat layout (legacy)

        If no frontmatter is present the directory name (or filename
        minus extension) is used as the agent name.
        """
        directory = Path(path)
        if not directory.is_dir():
            return []

        agents: list[AgentDef] = []
        seen_names: set[str] = set()

        # Walk for AGENT.md files (directory-based layout).
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_AGENT_DIRS]
            if "AGENT.md" in files:
                agent_md = Path(root) / "AGENT.md"
                try:
                    text = agent_md.read_text(encoding="utf-8")
                    parsed = _parse_agent_frontmatter(text, agent_md.parent.name)
                    name = parsed["name"]
                    if name in seen_names:
                        continue
                    seen_names.add(name)

                    category = _get_category_from_path(agent_md, directory)
                    agents.append(_build_agent_def(parsed, source, category))
                except Exception:
                    logger.exception("Failed to load agent from %s", agent_md)

        # Flat .md files at the top level (legacy layout).
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(".md"):
                continue
            if entry.name == "AGENT.md":
                continue
            try:
                text = entry.read_text(encoding="utf-8")
                parsed = _parse_agent_frontmatter(text, entry.stem)
                name = parsed["name"]
                if name in seen_names:
                    continue
                seen_names.add(name)
                agents.append(_build_agent_def(parsed, source))
            except Exception:
                logger.exception("Failed to load agent from %s", entry)

        return agents

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
    # DB loading
    # ------------------------------------------------------------------

    @staticmethod
    async def _load_skills_from_db(
        session: Any,
        org_id: UUID,
        user_id: UUID | None,
        source: str,
    ) -> list[SkillDef]:
        """Load enabled skills from the ``skills`` table.

        Parameters
        ----------
        session:
            An ``AsyncSession`` (SQLAlchemy).
        org_id:
            Organisation to filter by.
        user_id:
            ``None`` for org-wide rows, or a specific user UUID.
        source:
            Value for :attr:`SkillDef.source` (``"org_db"`` or ``"user_db"``).
        """
        from sqlalchemy import select
        from surogates.db.models import Skill

        stmt = (
            select(Skill)
            .where(Skill.org_id == org_id)
            .where(Skill.enabled.is_(True))
        )
        if user_id is None:
            stmt = stmt.where(Skill.user_id.is_(None))
        else:
            stmt = stmt.where(Skill.user_id == user_id)

        result = await session.execute(stmt)
        rows = result.scalars().all()
        skills: list[SkillDef] = []
        for row in rows:
            try:
                skills.append(_skill_from_db_row(row, source))
            except Exception:
                logger.warning("Skipping malformed DB skill %s", row.name, exc_info=True)
        return skills

    @staticmethod
    async def _load_agents_from_db(
        session: Any,
        org_id: UUID,
        user_id: UUID | None,
        source: str,
    ) -> list[AgentDef]:
        """Load enabled sub-agent types from the ``agents`` table.

        Parameters
        ----------
        session:
            An ``AsyncSession`` (SQLAlchemy).
        org_id:
            Organisation to filter by.
        user_id:
            ``None`` for org-wide rows, or a specific user UUID.
        source:
            Value for :attr:`AgentDef.source` (``"org_db"`` or ``"user_db"``).
        """
        from sqlalchemy import select
        from surogates.db.models import Agent

        stmt = (
            select(Agent)
            .where(Agent.org_id == org_id)
            .where(Agent.enabled.is_(True))
        )
        if user_id is None:
            stmt = stmt.where(Agent.user_id.is_(None))
        else:
            stmt = stmt.where(Agent.user_id == user_id)

        result = await session.execute(stmt)
        rows = result.scalars().all()
        agents: list[AgentDef] = []
        for row in rows:
            try:
                agents.append(_agent_from_db_row(row, source))
            except Exception:
                logger.warning("Skipping malformed DB agent %s", row.name, exc_info=True)
        return agents

    # ------------------------------------------------------------------
    # Merge logic
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(*layers: list[SkillDef | MCPServerDef | AgentDef]) -> list[Any]:
        """Merge layers with last-wins-by-name precedence.

        Layers are given in ascending priority order: the last layer's
        items override earlier ones with the same ``name``.
        """
        merged: dict[str, Any] = {}
        for layer in layers:
            for item in layer:
                merged[item.name] = item
        return list(merged.values())


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _skill_from_db_row(row: Any, source: str) -> SkillDef:
    """Convert a :class:`~surogates.db.models.Skill` ORM row to a :class:`SkillDef`.

    DB columns supply the primary fields.  Optional activation and expert
    fields (``trigger``, ``tags``, ``platforms``, ``expert_tools``, etc.)
    are stored in the ``config`` JSONB column.

    The ``content`` column may contain the full ``SKILL.md`` text
    including frontmatter; we reuse :func:`_parse_skill_frontmatter` to
    extract the body consistently.
    """
    cfg = row.config or {}

    # Reuse the canonical frontmatter parser to extract the body.
    parsed = _parse_skill_frontmatter(row.content or "", row.name)
    body = parsed["content"]

    # DB columns are authoritative for name/description/type; cfg JSONB
    # supplies activation and expert-loop metadata.
    return _build_skill_def(
        {
            "name": row.name,
            "description": row.description or "",
            "content": body,
            "type": row.type or "skill",
            "category": cfg.get("category"),
            "tags": cfg.get("tags"),
            "platforms": cfg.get("platforms"),
            "fallback_for_tools": cfg.get("fallback_for_tools"),
            "requires_tools": cfg.get("requires_tools"),
            "trigger": cfg.get("trigger"),
            "expert_model": row.expert_model,
            "expert_endpoint": row.expert_endpoint,
            "expert_adapter": row.expert_adapter,
            "expert_max_iterations": (
                row.expert_config.get("max_iterations", 10)
                if row.expert_config else 10
            ),
            "expert_status": row.expert_status or EXPERT_STATUS_DRAFT,
            "expert_tools": cfg.get("expert_tools"),
            "task_categories": cfg.get("task_categories"),
        },
        source=source,
        category=cfg.get("category"),
    )


def _agent_from_db_row(row: Any, source: str) -> AgentDef:
    """Convert a :class:`~surogates.db.models.Agent` ORM row to an :class:`AgentDef`.

    DB columns supply ``name``, ``description``, ``system_prompt``, and
    ``enabled``.  The ``config`` JSONB column supplies the remaining
    fields: ``tools``, ``disallowed_tools``, ``model``,
    ``max_iterations``, ``policy_profile``, ``category``, ``tags``.
    """
    cfg = row.config or {}

    max_iter = cfg.get("max_iterations")
    if max_iter is not None:
        try:
            max_iter = int(max_iter)
        except (TypeError, ValueError):
            max_iter = None

    tools = cfg.get("tools")
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    elif isinstance(tools, list):
        tools = [str(t) for t in tools]
    else:
        tools = None

    disallowed = cfg.get("disallowed_tools")
    if isinstance(disallowed, str):
        disallowed = [t.strip() for t in disallowed.split(",") if t.strip()]
    elif isinstance(disallowed, list):
        disallowed = [str(t) for t in disallowed]
    else:
        disallowed = None

    tags = cfg.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tags = [str(t).strip() for t in tags if t]
    else:
        tags = None

    return AgentDef(
        name=row.name,
        description=row.description or "",
        system_prompt=row.system_prompt or "",
        source=source,
        tools=tools,
        disallowed_tools=disallowed,
        model=cfg.get("model"),
        max_iterations=max_iter,
        policy_profile=cfg.get("policy_profile"),
        enabled=bool(row.enabled),
        category=cfg.get("category"),
        tags=tags,
    )


def _build_agent_def(
    parsed: dict[str, Any],
    source: str,
    category: str | None = None,
) -> AgentDef:
    """Construct an :class:`AgentDef` from parsed frontmatter data."""
    max_iter = parsed.get("max_iterations")
    if max_iter is not None:
        try:
            max_iter = int(max_iter)
        except (TypeError, ValueError):
            max_iter = None

    enabled = parsed.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("false", "0", "no", "off")

    return AgentDef(
        name=parsed["name"],
        description=parsed["description"],
        system_prompt=parsed["system_prompt"],
        source=source,
        tools=parsed.get("tools"),
        disallowed_tools=parsed.get("disallowed_tools"),
        model=parsed.get("model"),
        max_iterations=max_iter,
        policy_profile=parsed.get("policy_profile"),
        enabled=bool(enabled),
        category=category or parsed.get("category"),
        tags=parsed.get("tags"),
    )


def _parse_agent_frontmatter(
    text: str,
    fallback_name: str,
) -> dict[str, Any]:
    """Extract YAML frontmatter and body from an AGENT.md file.

    Returns a dict with keys: ``name``, ``description``, ``system_prompt``,
    and optional ``tools``, ``disallowed_tools``, ``model``,
    ``max_iterations``, ``policy_profile``, ``category``, ``tags``,
    ``enabled``.
    """
    result: dict[str, Any] = {
        "name": fallback_name,
        "description": "",
        "system_prompt": text,
    }

    stripped = text.strip()
    if stripped.startswith("---"):
        end_idx = stripped.find("---", 3)
        if end_idx != -1:
            frontmatter_text = stripped[3:end_idx].strip()
            result["system_prompt"] = stripped[end_idx + 3:].strip()

            fm = _parse_yaml_or_simple(frontmatter_text)
            result["name"] = fm.get("name", fallback_name)
            result["description"] = fm.get("description", "")

            # List-valued fields: accept YAML lists or comma-separated strings.
            for key in ("tools", "disallowed_tools", "tags"):
                val = fm.get(key)
                if val:
                    if isinstance(val, str):
                        result[key] = [
                            v.strip() for v in val.split(",") if v.strip()
                        ]
                    elif isinstance(val, list):
                        result[key] = [str(v).strip() for v in val if v]

            # Scalar fields.
            for key in ("model", "policy_profile", "category"):
                val = fm.get(key)
                if val:
                    result[key] = str(val)

            if "max_iterations" in fm:
                result["max_iterations"] = fm.get("max_iterations")

            if "enabled" in fm:
                result["enabled"] = fm.get("enabled")

            unknown = set(fm.keys()) - _KNOWN_AGENT_FRONTMATTER_KEYS
            if unknown:
                logger.warning(
                    "AGENT.md %r: unknown frontmatter keys %s -- ignored. "
                    "Check for typos (e.g. 'disallow_tools' vs 'disallowed_tools').",
                    fallback_name, sorted(unknown),
                )

    return result


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
        task_categories=parsed.get("task_categories") or [],
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

            task_categories = fm.get("task_categories")
            if task_categories:
                if isinstance(task_categories, str):
                    result["task_categories"] = [
                        t.strip().lower() for t in task_categories.split(",") if t.strip()
                    ]
                elif isinstance(task_categories, list):
                    result["task_categories"] = [
                        str(t).strip().lower() for t in task_categories if t
                    ]

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

    Preserves native Python types where the YAML parser returns them:

    * ``list`` / ``dict`` pass through untouched.
    * ``bool`` / ``int`` / ``float`` pass through untouched so callers
      like :func:`_build_agent_def` receive real booleans and numbers
      rather than the strings ``"True"`` / ``"5"``.
    * ``None`` keys are dropped so an empty value in the source (``tools:``)
      cannot surface as the literal string ``"None"`` (which previously
      parsed into a bogus one-element list ``["None"]``).
    * Everything else is coerced to ``str`` for parser stability --
      dates, tagged scalars, and similar would otherwise leak YAML
      library types into the consumers.
    """
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            result: dict[str, Any] = {}
            for k, v in data.items():
                if v is None:
                    continue
                if isinstance(v, (list, dict, bool, int, float)):
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


def _find_policy_profile_file(directory: Path, name: str) -> Path | None:
    """Find ``<name>.{yaml,yml,json}`` under *directory* or ``None``."""
    if not directory.is_dir():
        return None
    for suffix in (".yaml", ".yml", ".json"):
        candidate = directory / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


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
        transport=MCPTransport(cfg.get("transport", MCPTransport.STDIO.value)),
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
