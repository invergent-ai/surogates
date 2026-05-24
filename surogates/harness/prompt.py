"""System prompt builder -- assembles tenant-specific system prompts.

Constructs the full system prompt from tenant configuration, available
skills, user memory files, and platform metadata.  Prompt prose is not
hard-coded in this module: guidance blocks, platform hints, model-specific
addenda, and identity defaults are loaded from the :mod:`prompt_library`
markdown fragments under ``harness/prompts/``.

Includes an injection scanner that flags suspicious patterns in
externally-sourced content (memory, skill descriptions, agent bodies)
before it enters the prompt.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from surogates.harness.model_metadata import get_model_info
from surogates.harness.prompt_library import PromptLibrary, default_library
from surogates.tools.loader import AGENT_SOURCE_PLATFORM

if TYPE_CHECKING:
    from surogates.memory.manager import MemoryManager
    from surogates.session.models import Session
    from surogates.tenant.context import TenantContext
    from surogates.tools.loader import AgentDef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model routing constants (not prompt prose -- stays as Python config).
# ---------------------------------------------------------------------------

# Model name substrings that trigger the execution-discipline and
# tool-use-enforcement fragments.  These models benefit from explicit
# rules about verifying results, labelling assumptions, and executing
# promised actions instead of narrating them.  Claude and DeepSeek are
# *not* listed because they already do these things reliably; the prompt
# budget is better spent elsewhere for them.
#
# When you wire a new model into the platform, add its identifier (or a
# substring that matches it) here.  The failure mode of forgetting is
# silent — the model loads with no execution-discipline guidance and
# regresses on tasks like multi-source research, retry-on-empty, and
# uncertainty labelling — so prefer to add eagerly.
MODELS_REQUIRING_DISCIPLINE: tuple[str, ...] = (
    "gpt", "codex", "gemini", "gemma", "grok", "moonshot", "kimi",
    "surogate",
)

# Maximum bytes to read from any single memory/skill file.
_MAX_FILE_BYTES: int = 32_768

# ---------------------------------------------------------------------------
# Compiled injection detection patterns (case-insensitive).
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(previous|all|above|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*system\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\boverride\s+(system|instructions|rules)\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\s*:", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+if\s+you\s+are\b", re.IGNORECASE),
    re.compile(r"\bpretend\s+you\s+are\b", re.IGNORECASE),
    # Base64-encoded instruction smuggling: detect long base64 blocks that
    # decode to ASCII text containing suspicious keywords.
    re.compile(r"[A-Za-z0-9+/]{60,}={0,2}", re.ASCII),
]

# Keywords to look for inside decoded base64 payloads.
_B64_SUSPICIOUS_KEYWORDS: frozenset[str] = frozenset({
    "ignore",
    "system:",
    "override",
    "you are now",
    "instructions",
})


class PromptBuilder:
    """Builds system prompts from tenant config, skills, memory, and platform settings."""

    def __init__(
        self,
        tenant: TenantContext,
        skills: list | None = None,
        memory_manager: MemoryManager | None = None,
        session: Session | None = None,
        available_tools: set[str] | None = None,
        agent_def: AgentDef | None = None,
        available_agents: list[AgentDef] | None = None,
        available_kbs: list[dict] | None = None,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self.tenant = tenant
        self.skills: list = skills or []
        self._available_tools: set[str] = available_tools or set()
        self._memory_manager: MemoryManager | None = memory_manager
        self._session: Session | None = session
        # Active sub-agent type for the current session, or None when
        # the session runs with the default identity.  When set, the
        # agent def's system_prompt body replaces the org-level
        # personality/custom_instructions in the identity section.
        self._agent_def: AgentDef | None = agent_def
        # Catalog of enabled sub-agent types for the tenant.  Filtered
        # once at construction because disabled agents never render and
        # the list is immutable for the lifetime of the builder.
        self._available_agents: list[AgentDef] = [
            a for a in (available_agents or []) if a.enabled
        ]
        self.has_agents: bool = bool(self._available_agents)

        # Knowledge bases attached to this agent. Each entry is a dict
        # with keys: id, name, display_name, description. Rendered as
        # an "Available Knowledge Bases" block in the system prompt
        # so the LLM can pass kb_id to kb_list_pages / kb_read_page.
        self._available_kbs: list[dict] = list(available_kbs or [])
        self.has_kbs: bool = bool(self._available_kbs)
        # Cached rendered "# Available Sub-Agents" block.  The catalog
        # is immutable per builder and every description flows through
        # the regex injection scanner, so rendering once and reusing
        # across turns saves N×turns regex scans per wake cycle.
        self._available_agents_section_cache: str | None = None
        # Prompt fragment library (markdown + frontmatter).  Injected
        # primarily so tests can swap in a library backed by a temp
        # directory; production always uses the package-bundled default.
        self._prompts: PromptLibrary = prompt_library or default_library()

    def set_agent_def(self, agent_def: AgentDef | None) -> None:
        """Set or clear the active sub-agent type for the current session.

        Called by the harness after :func:`~surogates.harness.agent_resolver.resolve_agent_def`
        so the identity section and other builder output reflects the
        resolved agent type.  Pass ``None`` to restore default behaviour.
        """
        self._agent_def = agent_def

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> str:
        """Assemble the full system prompt.

        Layers:
        1. Agent identity (sub-agent body if active, otherwise org config or default)
        2. Working principles + heavy-thinking pattern (always loaded)
        3. Tool-aware behavioral guidance (memory, session_search, skills)
        4. Tool-use enforcement (model-specific)
        5. Memory (frozen snapshot)
        6. Skills index
        7. Available sub-agents (coordinator sessions only)
        8. Available experts (when any are active)
        9. Context files (AGENTS.md, .cursorrules)
        10. Timestamp, model info, platform hint
        11. Model-specific execution guidance (OpenAI, Google)
        """
        sections: list[str] = []
        sections.append(self._identity_section())
        sections.append(self._working_principles_section())

        # Tool-aware guidance — only injected when the tool is actually loaded.
        sections.append(self._tool_guidance_section())

        sections.append(self._memory_section())
        sections.append(self._skills_section())
        sections.append(self._preloaded_skills_section())
        sections.append(self._available_agents_section())
        sections.append(self._available_experts_section())
        sections.append(self._kb_section())
        sections.append(self._context_files_section())
        sections.append(self._context_section())

        # Model-specific guidance.
        model_id = self._get_model_id()
        sections.append(self._model_guidance_section(model_id))

        return "\n\n".join(s for s in sections if s)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _get_model_id(self) -> str:
        """Get the model ID from session or org config."""
        if self._session is not None and self._session.model:
            return self._session.model
        return self.tenant.org_config.get("default_model", "gpt-4o")

    def _tool_guidance_section(self) -> str:
        """Inject behavioral guidance based on which tools are loaded.
        Only injects guidance for tools that are actually available.
        """
        parts: list[str] = []

        if "memory" in self._available_tools:
            parts.append(self._prompts.get("guidance/memory"))
        if "session_search" in self._available_tools:
            parts.append(self._prompts.get("guidance/session_search"))
        if "skill_manage" in self._available_tools:
            parts.append(self._prompts.get("guidance/skills"))
        if "ask_user_question" in self._available_tools:
            parts.append(self._prompts.get("guidance/ask_user_question"))
        if "create_artifact" in self._available_tools:
            parts.append(self._prompts.get("guidance/artifact"))
        if any(tool.startswith("browser_") for tool in self._available_tools):
            parts.append(self._prompts.get("guidance/browser"))
        if (
            "loop_wait" in self._available_tools
            and self._session is not None
            and self._session.config.get("scheduled_dynamic_loop")
        ):
            parts.append(self._prompts.get("guidance/loop_wait"))
        elif (
            "loop_complete" in self._available_tools
            and self._session is not None
            and self._session.config.get("scheduled_session_id")
        ):
            # Non-dynamic scheduled children (fixed-cron ``/loop`` runs or
            # cron_create-spawned sessions) need to know they are already on
            # a schedule so they don't try to spawn another one.  The
            # guidance references ``loop_complete``, so we only inject it
            # when that tool is actually registered — otherwise a stale
            # worker process that has the new prompt fragment but not the
            # new tool registration would tell the LLM to call a tool
            # that doesn't exist.
            parts.append(self._prompts.get("guidance/cron_loop"))

        # Coordinator guidance — injected when the session is in coordinator mode.
        if (
            self._session is not None
            and self._session.config.get("coordinator")
        ):
            parts.append(self._prompts.get("guidance/coordinator"))

        # Execution discipline (verification, missing_context, etc.)
        # applies to any response from a discipline-required model — not
        # only ones that issue tool calls — so it loads on the model
        # match alone.  Tool-use enforcement is specifically about
        # narrate-vs-act on tool calls, so it additionally requires that
        # at least one tool be loaded.
        model_lower = self._get_model_id().lower()
        needs_discipline = any(
            p in model_lower for p in MODELS_REQUIRING_DISCIPLINE
        )
        if needs_discipline:
            parts.append(self._prompts.get("guidance/execution_discipline"))
            if self._available_tools:
                parts.append(self._prompts.get("guidance/tool_use_enforcement"))

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _working_principles_section(self) -> str:
        """Always-loaded working principles plus the heavy-thinking pattern.

        Two fragments rendered back-to-back: ``guidance/working_principles``
        (the 12-rule project charter — caution on non-trivial work, surface
        uncertainty over hiding it, conform to the codebase) and
        ``guidance/heavyskill`` (parallel-reason-then-synthesize pattern for
        hard reasoning problems, dispatched via ``delegate_task``).

        Both fragments are loaded unconditionally.  ``delegate_task`` is a
        built-in harness tool that is always registered, so gating
        heavyskill on tool availability adds noise without buying anything.
        Sub-agents inherit the same principles -- they are general behavior,
        not coordinator-specific.
        """
        return "\n\n".join((
            self._prompts.get("guidance/working_principles"),
            self._prompts.get("guidance/heavyskill"),
        ))

    def _identity_section(self) -> str:
        """Agent identity.

        When an active sub-agent type is set (``self._agent_def``), the
        agent def's ``system_prompt`` body replaces the org-level
        personality and custom_instructions.  The identity header
        switches to the agent name so the LLM knows which type it is.
        Platform-sourced agents are trusted; org/user-sourced agent
        bodies go through the injection scanner.
        """
        if self._agent_def is not None:
            agent = self._agent_def
            body = agent.system_prompt or ""
            if agent.source != AGENT_SOURCE_PLATFORM:
                body = self._sanitise(body, f"agent:{agent.name}")

            parts = [f"# Identity\nYou are **{agent.name}**."]
            if agent.description:
                parts.append(agent.description)
            if body.strip():
                parts.append(body)
            return "\n\n".join(p for p in parts if p)

        org_cfg = self.tenant.org_config

        agent_name: str = org_cfg.get("agent_name", "Surogate")
        personality: str = org_cfg.get(
            "personality",
            self._prompts.get("identity/default_personality"),
        )
        custom_instructions: str = org_cfg.get("custom_instructions", "")

        parts = [f"# Identity\nYou are **{agent_name}**."]
        parts.append(personality)
        if custom_instructions:
            safe = self._sanitise(custom_instructions, "custom_instructions")
            parts.append(safe)
        return "\n\n".join(parts)

    def _available_agents_section(self) -> str:
        """Render an "# Available Sub-Agents" block for coordinator sessions.

        Memoized after the first call because the agent catalog is
        immutable per builder and rendering runs regex injection scans
        on every description.  Returns the empty string when the
        session is not a coordinator or when no agents are configured.
        """
        if self._session is None:
            return ""
        if not self._session.config.get("coordinator"):
            return ""
        if not self._available_agents:
            return ""

        if self._available_agents_section_cache is not None:
            return self._available_agents_section_cache

        lines: list[str] = []
        for agent in self._available_agents:
            safe_desc = self._sanitise(
                agent.description or "", f"agent:{agent.name}",
            )
            entry = f"- **{agent.name}**"
            if safe_desc:
                entry += f" — {safe_desc}"
            if agent.tools:
                entry += f"\n  Tools: {', '.join(agent.tools)}"
            if agent.model:
                entry += f"\n  Model: {agent.model}"
            lines.append(entry)

        if not lines:
            self._available_agents_section_cache = ""
            return ""

        rendered = (
            "# Available Sub-Agents\n"
            "Pass ``agent_type=<name>`` to ``spawn_worker`` or "
            "``delegate_task`` to use one of these pre-configured sub-agent types. "
            "Each sub-agent runs with its own system prompt, tool filter, and model.\n"
            + "\n".join(lines)
        )
        self._available_agents_section_cache = rendered
        return rendered

    def _available_experts_section(self) -> str:
        """Render the ``# Available Experts`` system-prompt block.

        Lists active experts (``type=expert`` and ``expert_status="active"``)
        with their description and trigger phrases. The LLM uses
        ``consult_expert(expert, task)`` to consult one; the block also
        disambiguates from ``delegate_task`` which has a different
        purpose (multi-step sub-agent work in a fresh session).

        Returns an empty string when no active experts are loaded so the
        section is omitted entirely from the prompt.
        """
        from surogates.tools.builtin.expert import get_active_experts
        from surogates.tools.loader import SkillDef

        # ``skills`` may carry dicts (test helpers) alongside SkillDef
        # objects; filter to SkillDefs since get_active_experts inspects
        # the ``is_active_expert`` property.
        skill_defs = [s for s in self.skills if isinstance(s, SkillDef)]
        active = get_active_experts(skill_defs)
        if not active:
            return ""

        lines: list[str] = []
        for expert in active:
            safe_desc = self._sanitise(
                expert.description or "", f"expert:{expert.name}",
            )
            entry = f"- **{expert.name}**"
            if safe_desc:
                entry += f" — {safe_desc}"
            if expert.trigger:
                safe_trigger = self._sanitise(
                    expert.trigger, f"expert_trigger:{expert.name}",
                )
                entry += f"\n  Specialty: {safe_trigger}"
            lines.append(entry)

        return (
            "# Available Experts\n"
            "Specialist models you can consult for focused domain work. "
            "Call `consult_expert(expert, task)` when a request falls "
            "within an expert's specialty — for example, a SQL writer for "
            "query-shaped questions or a code reviewer for inspecting a "
            "file. Do NOT use `delegate_task` for this — that tool spawns "
            "sub-agents for multi-step work in a fresh session; experts "
            "are single-shot specialists.\n\n"
            + "\n".join(lines)
        )

    def _memory_section(self) -> str:
        """Load memory from MemoryManager frozen snapshot (if available) or fall back to file read."""
        # Try MemoryManager first.
        if self._memory_manager is not None:
            block = self._memory_manager.build_system_prompt()
            if block:
                # Still include user preferences alongside the managed memory.
                parts: list[str] = [block]
                prefs = self.tenant.user_preferences
                if prefs:
                    pref_lines = [f"- **{k}**: {v}" for k, v in prefs.items()]
                    parts.append("## User Preferences\n" + "\n".join(pref_lines))
                return "# Memory\n\n" + "\n\n".join(parts)

        # Fall back to direct file read (user-scoped memory directory).
        asset_root = Path(self.tenant.asset_root)
        memory_dir = asset_root / "users" / str(self.tenant.user_id) / "memory"

        fragments: list[str] = []

        for filename in ("MEMORY.md", "USER.md"):
            path = memory_dir / filename
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")[:_MAX_FILE_BYTES]
            except OSError:
                logger.warning("Failed to read memory file %s", path)
                continue
            content = self._sanitise(content, str(path))
            if content.strip():
                fragments.append(f"## {filename}\n{content}")

        # Also support per-user preferences from the tenant context.
        prefs = self.tenant.user_preferences
        if prefs:
            pref_lines = [f"- **{k}**: {v}" for k, v in prefs.items()]
            fragments.append("## User Preferences\n" + "\n".join(pref_lines))

        if not fragments:
            return ""
        return "# Memory\n\n" + "\n\n".join(fragments)

    def _skills_section(self) -> str:
        """Index executor-visible skills with descriptions."""
        if not self.skills:
            return ""

        skills_by_category: dict[str, list[tuple[str, str]]] = {}
        category_descriptions: dict[str, str] = {}

        from surogates.tools.loader import SkillDef

        for skill in self.skills:
            if isinstance(skill, dict):
                name = skill.get("name", "unnamed")
                desc = skill.get("description", "")
                skill_type = skill.get("type", "skill")
                category = skill.get("category") or "general"
                category_description = skill.get("category_description") or ""
                fallback_for_tools = self._as_string_list(
                    skill.get("fallback_for_tools"),
                )
                requires_tools = self._as_string_list(skill.get("requires_tools"))
            elif isinstance(skill, SkillDef):
                name = skill.name
                desc = skill.description
                skill_type = skill.type
                category = skill.category or "general"
                category_description = skill.category_description or ""
                fallback_for_tools = self._as_string_list(skill.fallback_for_tools)
                requires_tools = self._as_string_list(skill.requires_tools)
            else:
                name = str(skill)
                desc = ""
                skill_type = "skill"
                category = "general"
                category_description = ""
                fallback_for_tools = []
                requires_tools = []

            safe_desc = self._sanitise(desc, f"skill:{name}")

            if skill_type == "expert":
                continue
            else:
                if not self._skill_visible(fallback_for_tools, requires_tools):
                    continue
                safe_category = self._sanitise(category, f"skill_category:{category}")
                safe_category_desc = self._sanitise(
                    category_description, f"skill_category:{category}",
                )
                if safe_category_desc:
                    category_descriptions.setdefault(safe_category, safe_category_desc)
                skills_by_category.setdefault(safe_category, []).append((name, safe_desc))

        sections: list[str] = []
        if skills_by_category:
            sections.append(self._regular_skills_prompt(
                skills_by_category,
                category_descriptions,
            ))

        return "\n\n".join(sections)

    def _preloaded_skills_section(self) -> str:
        """Inline the full SKILL.md body of every skill named in
        ``session.config["preloaded_skills"]``.

        Unlike the catalog rendering above (which only shows name +
        description), this section emits the entire skill content so
        the agent has the playbook in its system prompt without having
        to call ``skill_view``. Used to auto-enforce role-specific
        playbooks like ``subagent-task-worker`` for sessions running a
        Task — set by ``surogates.tasks.spawn._build_task_worker_config``.

        Returns an empty string when ``preloaded_skills`` is unset or
        empty, or when no listed name resolves against the tenant skill
        catalog (logged at debug; missing skills don't fail the wake).
        """
        if self._session is None:
            return ""
        cfg = getattr(self._session, "config", None) or {}
        names = cfg.get("preloaded_skills") or []
        if not names:
            return ""

        from surogates.tools.loader import SkillDef

        wanted = set(names)
        bodies: list[str] = []
        for skill in self.skills:
            if isinstance(skill, SkillDef):
                name, content = skill.name, skill.content
            elif isinstance(skill, dict):
                name = skill.get("name") or ""
                content = skill.get("content") or ""
            else:
                continue
            if name in wanted and content:
                safe = self._sanitise(content, f"skill:{name}")
                bodies.append(f"## {name}\n\n{safe}")
                wanted.discard(name)

        if wanted:
            logger.debug(
                "preloaded_skills not found in tenant catalog: %s",
                sorted(wanted),
            )
        if not bodies:
            return ""
        return "# Loaded Skills\n\n" + "\n\n".join(bodies)

    def _skill_visible(
        self,
        fallback_for_tools: list[str],
        requires_tools: list[str],
    ) -> bool:
        """Return whether a regular skill should appear for this toolset."""
        if fallback_for_tools and all(
            tool in self._available_tools for tool in fallback_for_tools
        ):
            return False
        if requires_tools and not all(
            tool in self._available_tools for tool in requires_tools
        ):
            return False
        return True

    def _regular_skills_prompt(
        self,
        skills_by_category: dict[str, list[tuple[str, str]]],
        category_descriptions: dict[str, str],
    ) -> str:
        """Render regular skills in the Hermes-style mandatory index."""
        index_lines: list[str] = []
        for category in sorted(skills_by_category):
            cat_desc = category_descriptions.get(category, "")
            if cat_desc:
                index_lines.append(f"  {category}: {cat_desc}")
            else:
                index_lines.append(f"  {category}:")

            seen_names: set[str] = set()
            for name, desc in sorted(
                skills_by_category[category],
                key=lambda item: item[0],
            ):
                if name in seen_names:
                    continue
                seen_names.add(name)
                if desc:
                    index_lines.append(f"    - {name}: {desc}")
                else:
                    index_lines.append(f"    - {name}")

        management_hint = ""
        if "skill_manage" in self._available_tools:
            management_hint = (
                "\nIf a skill has issues, fix it with "
                "`skill_manage(action='patch')`."
            )

        return (
            "# Available Skills\n"
            "## Skills (mandatory)\n"
            "Before replying, scan the skills below. If a skill matches or is "
            "even partially relevant to your task, you MUST load it with "
            "`skill_view(name)` and follow its instructions. Err on the side "
            "of loading: skills contain specialized commands, workflows, "
            "project conventions, and quality standards that may differ from "
            "general-purpose knowledge."
            f"{management_hint}\n\n"
            "<available_skills>\n"
            + "\n".join(index_lines)
            + "\n</available_skills>\n\n"
            "Only proceed without loading a skill if genuinely none are relevant "
            "to the task."
        )

    @staticmethod
    def _as_string_list(value: Any) -> list[str]:
        """Normalize optional list-ish skill metadata."""
        if not value:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    def _context_section(self) -> str:
        """Timestamp, model info, platform hints."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        parts = [
            "# Context",
            f"- **Current date/time**: {now}",
            f"- **Organisation**: {self.tenant.org_id}",
        ]

        model_id = self._get_model_id()
        model_info = get_model_info(model_id)
        if model_info is not None:
            parts.append(f"- **Model**: {model_info.id}")
            parts.append(
                f"- **Context window**: {model_info.context_window:,} tokens"
            )
            if model_info.supports_vision:
                parts.append(
                    "- **Vision**: supported — you can see images attached "
                    "to user messages directly. Do not use vision_analyze "
                    "for images already in the conversation."
                )
        else:
            parts.append(f"- **Model**: {model_id}")

        parts.append(
            "- **Platform**: Surogates managed agent platform "
            "(multi-tenant, K8s-native)"
        )

        # Workspace path — tells the LLM where it is and that it must
        # stay within the workspace.  The leading newline keeps a blank
        # line between the preceding bullet and the Workspace Rules
        # heading after the final ``"\n".join(parts)``.
        workspace = self._get_workspace_path()
        if workspace:
            parts.append(f"- **Workspace**: `$HOME` (your working directory)")
            parts.append("\n" + self._prompts.get("identity/workspace_rules"))

        # Platform hint based on session channel.
        channel = self._get_channel()
        if channel:
            hint = self._prompts.platform_hint(channel)
            if hint:
                parts.append(f"\n## Platform\n{hint}")

        return "\n".join(parts)

    def _kb_section(self) -> str:
        """Render attached knowledge bases as a system prompt block.

        Empty string when no KBs are attached -- the section is dropped
        cleanly by the join filter in build(). The IDs are rendered
        verbatim so the LLM can pass them to kb_list_pages and
        kb_read_page without guesswork.
        """
        if not self._available_kbs:
            return ""
        lines = ["# Available Knowledge Bases", ""]
        lines.append(
            "You have access to the following knowledge bases. Use "
            "`kb_list_pages` to see the structure of a KB and "
            "`kb_read_page` to read individual pages. Pass the `id` "
            "value below as the `kb_id` argument."
        )
        lines.append("")
        for kb in self._available_kbs:
            kb_id = kb.get("id", "")
            display_name = kb.get("display_name") or kb.get("name", "")
            description = (kb.get("description") or "").strip()
            lines.append(f"- **{display_name}** (id: `{kb_id}`)")
            if description:
                lines.append(f"  {description}")
        return "\n".join(lines)

    def _context_files_section(self) -> str:
        """Load context files (SOUL.md, AGENTS.md, etc.) into the prompt."""
        from surogates.harness.context_files import load_project_context, load_soul_md

        parts: list[str] = []

        soul = load_soul_md(self.tenant.asset_root)
        if soul:
            parts.append(f"## Agent Identity (SOUL.md)\n{soul}")

        workspace = self._get_workspace_path()
        if workspace:
            project_ctx = load_project_context(workspace)
            if project_ctx:
                parts.append(f"## Project Context\n{project_ctx}")

        if not parts:
            return ""
        return "# Context Files\n\n" + "\n\n".join(parts)

    def _model_guidance_section(self, model_id: str) -> str:
        """Model-specific execution guidance based on model ID pattern matching.

        Note: model-agnostic execution discipline (verification, missing
        context, mandatory tool use) is injected by
        ``_tool_guidance_section`` via ``guidance/execution_discipline``.
        This method adds *provider-specific* addenda only — currently
        just Google's operational quirks (absolute paths, parallel tool
        calls, non-interactive flags).
        """
        if not model_id:
            return ""

        model_lower = model_id.lower()
        parts: list[str] = []

        # Google-specific operational guidance.
        if any(p in model_lower for p in ("gemini", "gemma")):
            parts.append(self._prompts.get("models/google"))

        return "\n\n".join(parts)

    def _get_channel(self) -> str | None:
        """Extract channel from session or return None."""
        if self._session is not None:
            return getattr(self._session, "channel", None)
        return None

    def _get_workspace_path(self) -> str | None:
        """Extract workspace path from session config or return None."""
        if self._session is not None:
            return self._session.config.get("workspace_path")
        return None

    # ------------------------------------------------------------------
    # Injection scanning
    # ------------------------------------------------------------------

    @staticmethod
    def scan_for_injection(content: str) -> bool:
        """Check for prompt injection patterns in memory/skill content.

        Returns ``True`` if suspicious content is detected.
        """
        for pattern in _INJECTION_PATTERNS:
            match = pattern.search(content)
            if match is None:
                continue

            matched_text = match.group(0)

            # For the base64 regex we need a secondary check: decode and
            # inspect the payload for suspicious keywords.
            if len(matched_text) >= 60 and re.fullmatch(
                r"[A-Za-z0-9+/]{60,}={0,2}", matched_text
            ):
                if _base64_looks_suspicious(matched_text):
                    return True
                # Long base64 that does not decode to suspicious text is
                # probably a legitimate data blob.
                continue

            # All other patterns are direct matches.
            return True

        return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sanitise(self, content: str, source_label: str) -> str:
        """Scan *content* for injections, stripping or warning as needed."""
        if self.scan_for_injection(content):
            logger.warning(
                "Prompt injection pattern detected in %s; content sanitised",
                source_label,
            )
            return "[content removed: suspicious injection pattern detected]"
        return content


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _base64_looks_suspicious(candidate: str) -> bool:
    """Attempt to decode a base64 candidate and check for suspicious keywords."""
    try:
        decoded = base64.b64decode(candidate, validate=True).decode("utf-8", errors="replace")
    except Exception:
        return False
    lowered = decoded.lower()
    return any(kw in lowered for kw in _B64_SUSPICIOUS_KEYWORDS)
