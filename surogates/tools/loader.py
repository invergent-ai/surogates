"""Resource loader for skills and sub-agent definitions.

Merges resources from three layers with increasing precedence:

1. **Platform bundle** -- the per-agent bundle fetched from Surogate Hub
   (skills/ + agents/ prefixes), passed in by the caller.
2. **User files** -- stored under the tenant's per-user asset root
   (managed by end users via the agent's ``skill_manage`` tool).
3. **Org DB** -- ``skills`` table rows where ``user_id IS NULL``
   (managed by org admin via API).
4. **User DB** -- ``skills`` table rows where ``user_id`` matches
   (managed by org admin via API).

Org admin overrides (DB layers) are final -- end users cannot override
them via bucket files.  MCP servers are not loaded here; the MCP proxy
reads the ``mcp_servers`` table directly.
"""

from __future__ import annotations

import asyncio
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

    When ``type`` is ``"expert"``, the skill configures a
    task-specialized reasoning model.  The harness may consult active
    experts automatically for matching hard tasks using the skill
    trigger, and the base LLM can also delegate explicitly via
    ``consult_expert``.
    """

    name: str
    description: str
    content: str  # The SKILL.md body (everything after the frontmatter)
    source: str  # "platform", "user", "org_db", "user_db"
    type: str = "skill"  # "skill" (prompt-based) or "expert" (model-backed)
    category: str | None = None  # subdirectory grouping
    tags: list[str] | None = None  # metadata tags
    # Conditional activation fields (parsed from frontmatter).
    platforms: list[str] | None = None  # e.g. ["linux", "macos"]
    fallback_for_tools: list[str] | None = None  # show only when these tools are unavailable
    requires_tools: list[str] | None = None  # show only when these tools ARE available
    trigger: str | None = None  # trigger description
    # Expert-specific fields (None/default for regular skills).
    expert_model: str | None = None  # model name (e.g. "claude-sonnet-4-6")
    expert_endpoint: str | None = None  # OpenAI-compatible inference URL
    expert_adapter: str | None = None  # LoRA adapter path in tenant storage
    expert_max_iterations: int = 10  # iteration budget for expert mini-loop
    expert_status: str = EXPERT_STATUS_DRAFT  # draft → collecting → active → retired
    expert_tools: list[str] | None = None  # tools the expert can use in its mini-loop
    category_description: str | None = None  # DESCRIPTION.md text for grouping

    @property
    def is_expert(self) -> bool:
        """Return ``True`` if this skill configures an expert model."""
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


# ------------------------------------------------------------------
# Loader
# ------------------------------------------------------------------


class ResourceLoader:
    """Loads skills and sub-agent definitions from the per-agent bundle,
    tenant asset roots, and the surogates DB."""

    def __init__(self) -> None:
        # Stateless — kept as a class for namespacing and DI in tests.
        pass

    @classmethod
    def from_settings(cls, settings: Any | None) -> "ResourceLoader":
        """Construct a loader.  Kept for call-site stability after the
        platform-directory settings were retired; ``settings`` is unused."""
        return cls()

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    async def load_skills(
        self,
        tenant: Any,
        db_session: Any | None = None,
        bundle: Any | None = None,
        system_bundle: Any | None = None,
    ) -> list[SkillDef]:
        """Merge skills from the system bundle, per-agent bundle, user
        files, and DB layers.

        Layer precedence (lowest → highest):

        1a. System bundle (``platform/system-skills``, shared across
            every agent in the cluster — same snapshot, no per-agent
            copy)
        1b. Per-agent bundle (``skills/`` prefix in the agent's Hub
            bundle, populated by org admins via the ops attach UI)
        2.  User bucket files (``tenant-{org}/users/{user}/skills/``)
        3.  Org-wide DB rows (``skills`` table, ``user_id IS NULL``)
        4.  User-specific DB rows (``skills`` table, ``user_id = ?``)

        Org admin overrides (DB layers) are final.  Per-agent (1b)
        shadows system (1a) by name because the per-agent bundle is
        passed last to ``_merge`` — see the design doc
        ``docs/superpowers/specs/2026-06-03-system-skills-shared-bundle-design.md``
        for the override semantics rationale.
        """
        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        # Service-account principals reach this method with
        # ``user_id=None`` — the user-files and user-DB layers don't
        # apply to them, so skip both rather than synthesising a
        # ``"None"`` path component or filtering DB rows on a literal
        # string.
        user_id = tenant.user_id

        # Layer 1a: shared system-skills bundle (one snapshot for the
        # whole cluster, served at the repo root).
        if system_bundle is not None:
            system = await self._load_skills_from_bundle(
                system_bundle,
                source=SKILL_SOURCE_PLATFORM,
                root_prefix="",
            )
        else:
            system = []

        # Layer 1b: per-agent bundle (org-attached skills under
        # ``skills/`` in the agent's bundle repo).
        if bundle is not None:
            per_agent = await self._load_skills_from_bundle(
                bundle,
                source=SKILL_SOURCE_PLATFORM,
                root_prefix="skills/",
            )
        else:
            per_agent = []

        # Per-agent wins over system on name collision because
        # ``_merge`` keeps the last entry seen for a given name.
        platform = self._merge(system, per_agent)

        # Layer 2: user bucket files
        if user_id is not None:
            user_skills_dir = str(
                asset_root / org_id / "users" / str(user_id) / "skills"
            )
            user_files = await asyncio.to_thread(
                self._load_skills_from_dir,
                user_skills_dir, SKILL_SOURCE_USER,
            )
        else:
            user_files = []

        if db_session is not None:
            # Layer 3: org-wide DB entries
            org_db = await self._load_skills_from_db(
                db_session, tenant.org_id, user_id=None, source=SKILL_SOURCE_ORG_DB,
            )
            # Layer 4: user-specific DB entries — ``user_id=None`` short-
            # circuits the per-user filter, returning no rows.
            if user_id is not None:
                user_db = await self._load_skills_from_db(
                    db_session, tenant.org_id, user_id, source=SKILL_SOURCE_USER_DB,
                )
            else:
                user_db = []
            return self._merge(platform, user_files, org_db, user_db)

        # Fallback for the tests / paths that don't pass a session: just
        # the bundle + user-file merge.
        return self._merge(platform, user_files)

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
    # Sub-agent types
    # ------------------------------------------------------------------

    async def load_agents(
        self,
        tenant: Any,
        db_session: Any | None = None,
        bundle: Any | None = None,
    ) -> list[AgentDef]:
        """Merge sub-agent types from the bundle, user files, and DB.

        Layer precedence (lowest → highest):

        1. Platform bundle (``agents/`` prefix in the per-agent bundle)
        2. User bucket files (``tenant-{org}/users/{user}/agents/``)
        3. Org-wide DB rows (``agents`` table, ``user_id IS NULL``)
        4. User-specific DB rows (``agents`` table, ``user_id = ?``)

        Org admin overrides (DB layers) are final -- end users cannot
        override them via bucket files.
        """
        asset_root = Path(tenant.asset_root)
        org_id = str(tenant.org_id)
        user_id = str(tenant.user_id)

        user_agents_dir = str(
            asset_root / org_id / "users" / user_id / "agents"
        )

        # Layer 1: bundle sub-agents (per-agent, served from Surogate Hub).
        if bundle is not None:
            platform = await self._load_agents_from_bundle(
                bundle, source=AGENT_SOURCE_PLATFORM,
            )
        else:
            platform = []

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

        # Fallback for the tests / paths that don't pass a session.
        return self._merge(platform, user_files)

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
        category_descriptions = _load_category_descriptions(directory)

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
                        _build_skill_def(
                            parsed,
                            source,
                            category,
                            category_descriptions.get(category or ""),
                        ),
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

    async def _load_skills_from_bundle(
        self,
        bundle: Any,
        *,
        source: str,
        root_prefix: str = "skills/",
    ) -> list[SkillDef]:
        """Read platform skills from a Hub-backed bundle.

        ``root_prefix`` selects where in the bundle the skill catalog
        lives.  Per-agent bundles store skills under ``skills/<name>/``
        (default).  The shared system-skills bundle
        (``platform/system-skills``) stores them at the repo root, so
        callers pass ``""``.

        Iterates ``<root_prefix><name>/SKILL.md`` and builds the same
        :class:`SkillDef` objects the on-disk loader produces.  A Hub
        failure on ``list`` does not propagate — Layer 1 falls back to
        an empty list so the rest of the layers still resolve.
        """
        try:
            paths = await bundle.list(root_prefix)
        except Exception:
            logger.exception(
                "Failed to list bundle '%s'; falling back to empty Layer 1",
                root_prefix or "<root>",
            )
            return []

        skills: list[SkillDef] = []
        seen_names: set[str] = set()
        for path in paths:
            if not path.endswith("/SKILL.md"):
                continue
            # ``<root_prefix><dir>/SKILL.md`` → ``<dir>``
            inner = path[len(root_prefix):-len("/SKILL.md")]
            if not inner:
                continue
            try:
                text = await bundle.read_text(path)
            except LookupError:
                continue
            try:
                parsed = _parse_skill_frontmatter(text, inner.split("/")[-1])
                name = parsed["name"]
                if name in seen_names:
                    continue
                seen_names.add(name)
                skills.append(_build_skill_def(parsed, source))
            except Exception:
                logger.exception(
                    "Failed to parse bundle skill at %s", path,
                )
        return skills

    # ------------------------------------------------------------------
    # Sub-agent parsing
    # ------------------------------------------------------------------

    async def _load_agents_from_bundle(
        self, bundle: Any, *, source: str,
    ) -> list[AgentDef]:
        """read platform sub-agent definitions
        from the agent's Hub-backed bundle.

        Iterates ``bundle/agents/*/AGENT.md`` and constructs the same
        AgentDef objects the on-disk loader produces.
        """
        try:
            paths = await bundle.list("agents/")
        except Exception:
            logger.exception(
                "Failed to list bundle agents/; falling back to empty layer 1",
            )
            return []

        agents: list[AgentDef] = []
        seen_names: set[str] = set()
        for path in paths:
            if not path.endswith("/AGENT.md"):
                continue
            inner = path[len("agents/"):-len("/AGENT.md")]
            if not inner:
                continue
            try:
                text = await bundle.read_text(path)
            except LookupError:
                continue
            try:
                parsed = _parse_agent_frontmatter(text, inner.split("/")[-1])
                name = parsed["name"]
                if name in seen_names:
                    continue
                seen_names.add(name)
                agents.append(_build_agent_def(parsed, source))
            except Exception:
                logger.exception(
                    "Failed to parse bundle agent at %s", path,
                )
        return agents

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
    category_description: str | None = None,
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
        category_description=category_description,
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
                if isinstance(trigger, list):
                    result["trigger"] = ", ".join(str(t).strip() for t in trigger if t)
                else:
                    result["trigger"] = str(trigger)

            # Skill type: "skill" (default) or "expert" (model-backed).
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

    For paths like ``skills_dir/mlops/axolotl/SKILL.md`` return
    ``"mlops"``.  Nested category paths are preserved:
    ``skills_dir/mlops/evaluation/bench/SKILL.md`` returns
    ``"mlops/evaluation"``.
    """
    try:
        rel_path = skill_md.relative_to(skills_dir)
        parts = rel_path.parts
        # parts = ("category", "skill-name", "SKILL.md") -> category
        if len(parts) >= 3:
            return "/".join(parts[:-2])
    except ValueError:
        pass
    return None


def _load_category_descriptions(skills_dir: Path) -> dict[str, str]:
    """Return category descriptions from ``DESCRIPTION.md`` files.

    Supports both Hermes-style frontmatter:

    ``---\ndescription: ...\n---``

    and plain Markdown body files.  The category key is the path from
    ``skills_dir`` to the directory containing ``DESCRIPTION.md``.
    """
    descriptions: dict[str, str] = {}
    if not skills_dir.is_dir():
        return descriptions

    for root, dirs, files in os.walk(skills_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
        if "DESCRIPTION.md" not in files:
            continue
        desc_file = Path(root) / "DESCRIPTION.md"
        try:
            rel = desc_file.parent.relative_to(skills_dir)
        except ValueError:
            continue
        if rel.parts in ((), (".",)):
            continue

        try:
            text = desc_file.read_text(encoding="utf-8")
        except OSError:
            logger.debug("Could not read skill category description %s", desc_file)
            continue

        descriptions["/".join(rel.parts)] = _parse_description_text(text)

    return descriptions


def _parse_description_text(text: str) -> str:
    """Extract a compact description from a DESCRIPTION.md file."""
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.startswith("---"):
        end_idx = stripped.find("---", 3)
        if end_idx != -1:
            frontmatter_text = stripped[3:end_idx].strip()
            fm = _parse_yaml_or_simple(frontmatter_text)
            desc = fm.get("description")
            if desc:
                return str(desc).strip().strip("'\"")
            body = stripped[end_idx + 3:].strip()
            return body
    return stripped


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
