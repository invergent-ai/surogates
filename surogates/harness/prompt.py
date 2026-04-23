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

# Model name substrings that trigger the tool-use enforcement fragment.
# These models exhibit a "narrate the action instead of executing it"
# pattern (e.g. "I will now create an artifact" followed by end-of-turn
# with no tool call) often enough that the enforcement fragment pays for
# itself in prompt budget.  Claude and DeepSeek are *not* listed because
# they reliably execute promised actions without the nag.
TOOL_USE_ENFORCEMENT_MODELS: tuple[str, ...] = (
    "gpt", "codex", "gemini", "gemma", "grok", "moonshot", "kimi",
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
        2. Tool-aware behavioral guidance (memory, session_search, skills)
        3. Tool-use enforcement (model-specific)
        4. Memory (frozen snapshot)
        5. Skills index
        6. Available sub-agents (coordinator sessions only)
        7. Context files (AGENTS.md, .cursorrules)
        8. Timestamp, model info, platform hint
        9. Model-specific execution guidance (OpenAI, Google)
        """
        sections: list[str] = []
        sections.append(self._identity_section())

        # Tool-aware guidance — only injected when the tool is actually loaded.
        sections.append(self._tool_guidance_section())

        sections.append(self._memory_section())
        sections.append(self._skills_section())
        sections.append(self._available_agents_section())
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
        if "consult_expert" in self._available_tools:
            parts.append(self._prompts.get("guidance/expert"))
        if "create_artifact" in self._available_tools:
            parts.append(self._prompts.get("guidance/artifact"))

        # Coordinator guidance — injected when the session is in coordinator mode.
        if (
            self._session is not None
            and self._session.config.get("coordinator")
        ):
            parts.append(self._prompts.get("guidance/coordinator"))

        # Tool-use enforcement for models that tend to skip tools.
        if self._available_tools:
            model_lower = self._get_model_id().lower()
            if any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS):
                parts.append(self._prompts.get("guidance/tool_use_enforcement"))

        if not parts:
            return ""
        return "\n\n".join(parts)

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
        """Index available skills and experts with descriptions."""
        if not self.skills:
            return ""

        regular_skills: list[str] = []
        expert_lines: list[str] = []

        from surogates.tools.loader import SkillDef

        for skill in self.skills:
            if isinstance(skill, dict):
                name = skill.get("name", "unnamed")
                desc = skill.get("description", "")
                trigger = skill.get("trigger", "")
                skill_type = skill.get("type", "skill")
                expert_tools = skill.get("expert_tools") or []
                expert_stats = skill.get("expert_stats") or {}
            elif isinstance(skill, SkillDef):
                name = skill.name
                desc = skill.description
                trigger = skill.trigger or ""
                skill_type = skill.type
                expert_tools = skill.expert_tools or []
                expert_stats = {}
            else:
                name = str(skill)
                desc = ""
                trigger = ""
                skill_type = "skill"
                expert_tools = []
                expert_stats = {}

            safe_desc = self._sanitise(desc, f"skill:{name}")

            if skill_type == "expert":
                entry = f"- **{name}**"
                if safe_desc:
                    entry += f" — {safe_desc}"
                total_uses = expert_stats.get("total_uses", 0)
                total_successes = expert_stats.get("total_successes", 0)
                if total_uses > 0:
                    rate = (total_successes / total_uses) * 100
                    entry += f"\n  Success rate: {rate:.0f}% ({total_uses} uses)."
                if expert_tools:
                    entry += f"\n  Tools: {', '.join(expert_tools)}"
                expert_lines.append(entry)
            else:
                entry = f"- **{name}**"
                if safe_desc:
                    entry += f": {safe_desc}"
                if trigger:
                    entry += f" (trigger: {trigger})"
                regular_skills.append(entry)

        sections: list[str] = []
        if regular_skills:
            sections.append("# Available Skills\n" + "\n".join(regular_skills))
        if expert_lines:
            sections.append(
                "# Available Experts\n"
                "Use `consult_expert` to delegate tasks to these specialised models.\n"
                + "\n".join(expert_lines)
            )

        return "\n\n".join(sections)

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
                parts.append("- **Vision**: supported")
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

        Note: the generic tool-use-enforcement fragment is injected by
        ``_tool_guidance_section`` (conditional on available tools).  This
        method adds the *provider-specific* addenda only.
        """
        if not model_id:
            return ""

        model_lower = model_id.lower()
        parts: list[str] = []

        # OpenAI-specific execution discipline.
        if any(p in model_lower for p in ("gpt", "codex", "o3", "o4")):
            parts.append(self._prompts.get("models/openai"))

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
