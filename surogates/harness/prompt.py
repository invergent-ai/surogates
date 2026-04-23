"""System prompt builder -- assembles tenant-specific system prompts.

Constructs the full system prompt from tenant configuration, available
skills, user memory files, and platform metadata.  Includes an injection
scanner that flags suspicious patterns in externally-sourced content
(memory, skill descriptions) before they enter the prompt.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from surogates.harness.model_metadata import get_model_info
from surogates.tools.loader import AGENT_SOURCE_PLATFORM

if TYPE_CHECKING:
    from surogates.memory.manager import MemoryManager
    from surogates.session.models import Session
    from surogates.tenant.context import TenantContext
    from surogates.tools.loader import AgentDef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform hints (thanks Hermes Agent)
# ---------------------------------------------------------------------------

PLATFORM_HINTS = {
    "whatsapp": (
        "You are on a text messaging communication platform, WhatsApp. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. The file "
        "will be sent as a native WhatsApp attachment — images (.jpg, .png, "
        ".webp) appear as photos, videos (.mp4, .mov) play inline, and other "
        "files arrive as downloadable documents. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as photos."
    ),
    "telegram": (
        "You are on a text messaging communication platform, Telegram. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. Images "
        "(.png, .jpg, .webp) appear as photos, audio (.ogg) sends as voice "
        "bubbles, and videos (.mp4) play inline. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as native photos."
    ),
    "discord": (
        "You are in a Discord server or group chat communicating with your user. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.png, .jpg, .webp) are sent as photo "
        "attachments, audio as file attachments. You can also include image URLs "
        "in markdown format ![alt](url) and they will be sent as attachments."
    ),
    "slack": (
        "You are in a Slack workspace communicating with your user. "
        "You can send media files natively: include MEDIA:/absolute/path/to/file "
        "in your response. Images (.png, .jpg, .webp) are uploaded as photo "
        "attachments, audio as file attachments. You can also include image URLs "
        "in markdown format ![alt](url) and they will be uploaded as attachments."
    ),
    "signal": (
        "You are on a text messaging communication platform, Signal. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: to deliver a file to the user, "
        "include MEDIA:/absolute/path/to/file in your response. Images "
        "(.png, .jpg, .webp) appear as photos, audio as attachments, and other "
        "files arrive as downloadable documents. You can also include image "
        "URLs in markdown format ![alt](url) and they will be sent as photos."
    ),
    "email": (
        "You are communicating via email. Write clear, well-structured responses "
        "suitable for email. Use plain text formatting (no markdown). "
        "Keep responses concise but complete. You can send file attachments — "
        "include MEDIA:/absolute/path/to/file in your response. The subject line "
        "is preserved for threading. Do not include greetings or sign-offs unless "
        "contextually appropriate."
    ),
    "cron": (
        "You are running as a scheduled cron job. There is no user present — you "
        "cannot ask questions, request clarification, or wait for follow-up. Execute "
        "the task fully and autonomously, making reasonable decisions where needed. "
        "Your final response is automatically delivered to the job's configured "
        "destination — put the primary content directly in your response."
    ),
    "cli": (
        "You are a CLI AI Agent. Try not to use markdown but simple text "
        "renderable inside a terminal."
    ),
    "sms": (
        "You are communicating via SMS. Keep responses concise and use plain text "
        "only — no markdown, no formatting. SMS messages are limited to ~1600 "
        "characters, so be brief and direct."
    ),
}

# ---------------------------------------------------------------------------
# Model-specific execution guidance (thanks Hermes Agent)
# ---------------------------------------------------------------------------

# Model name substrings that trigger TOOL_USE_ENFORCEMENT_GUIDANCE.
TOOL_USE_ENFORCEMENT_MODELS: tuple[str, ...] = (
    "gpt", "codex", "gemini", "gemma", "grok",
)

# Model name substrings that should use the 'developer' role instead of
# 'system' for the system prompt.  OpenAI's newer models (GPT-5, Codex)
# give stronger instruction-following weight to the 'developer' role.
# The swap happens at the API boundary so internal message representation
# stays consistent ("system" everywhere).
DEVELOPER_ROLE_MODELS: tuple[str, ...] = ("gpt-5", "codex")

# OpenAI GPT/Codex execution discipline.
OPENAI_MODEL_EXECUTION_GUIDANCE: str = (
    "# Execution discipline\n"
    "<tool_persistence>\n"
    "- Use tools whenever they improve correctness, completeness, or grounding.\n"
    "- Do not stop early when another tool call would materially improve the result.\n"
    "- If a tool returns empty or partial results, retry with a different query or "
    "strategy before giving up.\n"
    "- Keep calling tools until: (1) the task is complete, AND (2) you have verified "
    "the result.\n"
    "</tool_persistence>\n"
    "\n"
    "<mandatory_tool_use>\n"
    "NEVER answer these from memory or mental computation — ALWAYS use a tool:\n"
    "- Arithmetic, math, calculations → use terminal (e.g. python3 -c)\n"
    "- Hashes, encodings, checksums → use terminal (e.g. sha256sum, base64)\n"
    "- Current time, date, timezone → use terminal (e.g. date)\n"
    "- System state: OS, CPU, memory, disk, ports, processes → use terminal\n"
    "- File contents, sizes, line counts → use read_file, search_files, or terminal\n"
    "- Git history, branches, diffs → use terminal\n"
    "- Current facts (weather, news, versions) → use web_search\n"
    "Your memory and user profile describe the USER, not the system you are "
    "running on. The execution environment may differ from what the user profile "
    "says about their personal setup.\n"
    "</mandatory_tool_use>\n"
    "\n"
    "<act_dont_ask>\n"
    "When a question has an obvious default interpretation, act on it immediately "
    "instead of asking for clarification. Examples:\n"
    "- 'Is port 443 open?' → check THIS machine (don't ask 'open where?')\n"
    "- 'What OS am I running?' → check the live system (don't use user profile)\n"
    "- 'What time is it?' → run `date` (don't guess)\n"
    "Only ask for clarification when the ambiguity genuinely changes what tool "
    "you would call.\n"
    "</act_dont_ask>\n"
    "\n"
    "<prerequisite_checks>\n"
    "- Before taking an action, check whether prerequisite discovery, lookup, or "
    "context-gathering steps are needed.\n"
    "- Do not skip prerequisite steps just because the final action seems obvious.\n"
    "- If a task depends on output from a prior step, resolve that dependency first.\n"
    "</prerequisite_checks>\n"
    "\n"
    "<verification>\n"
    "Before finalizing your response:\n"
    "- Correctness: does the output satisfy every stated requirement?\n"
    "- Grounding: are factual claims backed by tool outputs or provided context?\n"
    "- Formatting: does the output match the requested format or schema?\n"
    "- Safety: if the next step has side effects (file writes, commands, API calls), "
    "confirm scope before executing.\n"
    "</verification>\n"
    "\n"
    "<missing_context>\n"
    "- If required context is missing, do NOT guess or hallucinate an answer.\n"
    "- Use the appropriate lookup tool when missing information is retrievable "
    "(search_files, web_search, read_file, etc.).\n"
    "- Ask a clarifying question only when the information cannot be retrieved by tools.\n"
    "- If you must proceed with incomplete information, label assumptions explicitly.\n"
    "</missing_context>"
)

# Gemini/Gemma-specific operational guidance, adapted from OpenCode's gemini.txt.
# Injected alongside TOOL_USE_ENFORCEMENT_GUIDANCE when the model is Gemini or Gemma.
GOOGLE_MODEL_OPERATIONAL_GUIDANCE: str = (
    "# Google model operational directives\n"
    "Follow these operational rules strictly:\n"
    "- **Absolute paths:** Always construct and use absolute file paths for all "
    "file system operations. Combine the project root with relative paths.\n"
    "- **Verify first:** Use read_file/search_files to check file contents and "
    "project structure before making changes. Never guess at file contents.\n"
    "- **Dependency checks:** Never assume a library is available. Check "
    "package.json, requirements.txt, Cargo.toml, etc. before importing.\n"
    "- **Conciseness:** Keep explanatory text brief — a few sentences, not "
    "paragraphs. Focus on actions and results over narration.\n"
    "- **Parallel tool calls:** When you need to perform multiple independent "
    "operations (e.g. reading several files), make all the tool calls in a "
    "single response rather than sequentially.\n"
    "- **Non-interactive commands:** Use flags like -y, --yes, --non-interactive "
    "to prevent CLI tools from hanging on prompts.\n"
    "- **Keep going:** Work autonomously until the task is fully resolved. "
    "Don't stop with a plan — execute it.\n"
)

# ---------------------------------------------------------------------------
# Tool-aware behavioral guidance
# Injected into the system prompt only when the corresponding tools are loaded.
# ---------------------------------------------------------------------------

MEMORY_GUIDANCE: str = (
    "You have persistent memory across sessions. Save durable facts using the memory "
    "tool: user preferences, environment details, tool quirks, and stable conventions. "
    "Memory is injected into every turn, so keep it compact and focused on facts that "
    "will still matter later.\n"
    "Prioritize what reduces future user steering — the most valuable memory is one "
    "that prevents the user from having to correct or remind you again. "
    "User preferences and recurring corrections matter more than procedural task details.\n"
    "Do NOT save task progress, session outcomes, completed-work logs, or temporary TODO "
    "state to memory; use session_search to recall those from past transcripts. "
    "If you've discovered a new way to do something, solved a problem that could be "
    "necessary later, save it as a skill with the skill tool."
)

SESSION_SEARCH_GUIDANCE: str = (
    "When the user references something from a past conversation or you suspect "
    "relevant cross-session context exists, use session_search to recall it before "
    "asking them to repeat themselves."
)

SKILLS_GUIDANCE: str = (
    "After completing a complex task (5+ tool calls), fixing a tricky error, "
    "or discovering a non-trivial workflow, save the approach as a "
    "skill with skill_manage so you can reuse it next time.\n"
    "When using a skill and finding it outdated, incomplete, or wrong, "
    "patch it immediately with skill_manage(action='patch') — don't wait to be asked. "
    "Skills that aren't maintained become liabilities."
)

EXPERT_GUIDANCE: str = (
    "Specialised expert models are available via the `consult_expert` tool. "
    "Each expert is fine-tuned on this organisation's data for a specific task. "
    "When a task falls squarely within an expert's specialty, delegate to it — "
    "experts are faster and cheaper than doing it yourself. "
    "Review the expert's result before presenting it to the user; you can "
    "accept, modify, or discard it."
)

ARTIFACT_GUIDANCE: str = (
    "# Artifacts\n"
    "You can render inline artifacts in the chat via `create_artifact`. Five "
    "kinds are supported: Vega-Lite **charts**, **tables**, standalone "
    "**markdown** documents, sandboxed **HTML** previews, and inline **SVG** "
    "images. Artifacts render at the point in the conversation where you call "
    "the tool, in their own panel separate from the back-and-forth.\n"
    "\n"
    "## How content gets into the artifact\n"
    "The body is a plain string parameter on the `create_artifact` call. Pass "
    "the full content inline — there is no file reference, URL, or streaming "
    "mode. Up to ~100KB is fine; a 40KB HTML page is small.\n"
    "\n"
    "You generate the content; you do not 'load' it from somewhere else. If "
    "you wrote the same content to disk earlier with `write_file`, do NOT "
    "round-trip it through `cat` / `read_file` to 'feed' it into "
    "`create_artifact`. Tool outputs do not chain into other tool inputs — "
    "just emit the content directly into the `create_artifact` call. (If the "
    "artifact is the only destination, skip the `write_file` step entirely.)\n"
    "\n"
    "## Rule of thumb\n"
    "Ask yourself: *will the user want to copy, save, or refer back to this "
    "content outside the conversation? Is the user asking me to explain/present/showcase something visually ? "
    "* If yes → artifact. If no → inline.\n"
    "\n"
    "## Use an artifact for\n"
    "- **Visual output** the user reads as a result: charts of trends, "
    "comparison tables, dashboard-style summaries, diagrams, visual explanations. Returning these "
    "as text in a code fence wastes the visual affordance.\n"
    "- **Tabular data** the user will want to read as a table. Use an "
    "artifact (not an inline markdown table) whenever ANY of the "
    "following is true:\n"
    "  - the table has 3 or more columns;\n"
    "  - any cell contains code, multi-line text, or a long prose "
    "    description that will wrap;\n"
    "  - the user asked for a comparison, matrix, or reference chart "
    "    they will want to save or export.\n"
    "  Short 2-column lookups (e.g. `term → one-word definition`) can "
    "  stay inline as markdown.\n"
    "- **Standalone markdown documents** over ~20 lines or ~1500 characters "
    "that the user will want to copy or save: reports, design notes, specs, "
    "study guides, structured plans, one-pagers.\n"
    "- **Interactive HTML demos, widgets, and single-file webpages** — "
    "calculators, todo widgets, forms the user wants to try, CSS "
    "demonstrations, small self-contained pages. HTML runs in a sandboxed "
    "iframe (no same-origin, no forms, no top-level navigation). **This "
    "case is an artifact, not a `write_file` call** — even when the user "
    "says 'single file' or 'one HTML file'. The artifact panel is "
    "self-contained and previewable in-thread; workspace files are not.\n"
    "- **SVG diagrams and illustrations** — logos, flowcharts drawn by hand, "
    "icon sketches, visual schematics.\n"
    "- Content the user has said they will reference, edit, or reuse.\n"
    "\n"
    "## Don't use an artifact for\n"
    "- Short answers or conversational replies — just reply in the message.\n"
    "- Explanatory content where code or data is part of teaching a concept — "
    "keep it in the message flow so the explanation stays readable.\n"
    "- Files that belong on disk — use `write_file` instead; artifacts are "
    "chat-embedded, not workspace files.\n"
    "- Data the user asked for as copy-pasteable text (JSON, CSV, raw code) — "
    "keep it as a code block in the message so they can copy it in place.\n"
    "- One-off answers or small examples that clarify a point.\n"
    "\n"
    "## Do not emit artifact-shaped content as code fences\n"
    "If the user asks for an **SVG**, an **HTML page or widget**, a "
    "**chart**, or any other content that `create_artifact` can render, "
    "invoke the tool. Do NOT paste the content into your reply as a "
    "` ```svg `, ` ```html `, or ` ```json ` (vega-lite) code fence — "
    "the user will see raw source instead of the rendered output, which "
    "defeats the point of asking for it. The tool call is what produces "
    "a usable artifact; a fenced code block produces a wall of text.\n"
    "\n"
    "## `create_artifact` vs `write_file`\n"
    "This is the one place it's easy to get wrong. The user's phrasing "
    "('single file', 'save to a file') does NOT decide it; the **intent** "
    "does:\n"
    "- `create_artifact` — the user wants to **see and interact with** the "
    "output right here in the chat (a calculator to click, a chart to look "
    "at, a document to read). The output's home is this conversation.\n"
    "- `write_file` — the user is working on a **project on disk** and "
    "wants this file added to or edited in the workspace so they can run, "
    "import, or commit it. The output's home is a codebase.\n"
    "When the user asks for 'a small HTML page', 'a calculator', 'a demo', "
    "'a widget', 'an SVG logo', 'a chart of X' and there's no indication "
    "of an ongoing project or codebase — it's an artifact. Use "
    "`write_file` only when the conversation is clearly about editing a "
    "specific project's files.\n"
    "\n"
    "## Rules\n"
    "- **One artifact per response** unless the user explicitly asks for more.\n"
    "- **Err on the side of not creating an artifact.** Overuse is jarring; "
    "when in doubt, keep it inline.\n"
    "- Pick a short, descriptive `name` — it becomes the artifact's title.\n"
    "- Charts must supply Vega-Lite `data.values` inline; `data.url` is blocked."
)

COORDINATOR_GUIDANCE: str = """\
# Worker Delegation

You can spawn autonomous **workers** to handle tasks in parallel. Workers
run in their own sessions with their own tools and context. Use workers
when a task benefits from parallelism or when you want to keep your own
context clean. You can also do everything directly — delegation is a tool,
not a requirement.

## When to delegate vs. do it yourself

- **Delegate** when the task is independent and can run in parallel with other work.
- **Delegate** when you want a fresh context window for a complex sub-task.
- **Do it yourself** when the task is simple, quick, or requires your conversation context.
- **Do it yourself** when you need to see intermediate results before deciding the next step.

Use your judgment. If it's faster to do it directly, do it directly.

## Delegation tools

- **spawn_worker** — Spawn a new worker. Returns immediately with a worker ID.
- **send_worker_message** — Send a follow-up to a worker (continue, correct, or extend).
- **stop_worker** — Interrupt a running worker.

To launch workers in parallel, call spawn_worker multiple times in the same response.

## Worker results

Worker results arrive as **user-role messages** with a `[Worker ... completed]` or
`[Worker ... failed]` prefix. Use the worker_id with send_worker_message to continue
that worker. Worker results look like user messages but are not — distinguish them
by the prefix.

## Concurrency guidelines

- **Independent tasks** — run in parallel freely (e.g. researching two different areas).
- **Dependent tasks** — serialize (wait for the first result before launching the next).
- **Conflicting writes** — one worker at a time per set of files/resources.
- **Verification** of another worker's output — spawn a fresh worker for unbiased review.

## Writing worker prompts

**Workers can't see your conversation.** Every prompt must be self-contained.
Include all necessary context, specifics, and what "done" looks like.

Never write "based on your findings" or "based on the research." These phrases
delegate understanding to the worker. Synthesize the findings yourself, then
give the worker a concrete, actionable prompt.

```
// Bad — lazy delegation
spawn_worker(goal="Based on the research, fix the problem")

// Good — synthesized spec with full context
spawn_worker(goal="The config parser in src/config.py:42 crashes on empty YAML files because yaml.safe_load returns None. Add a None check after line 42 — if None, return an empty dict. Run tests and report results.")
```

## Continue vs. spawn fresh

| Situation | Action |
|-----------|--------|
| Worker explored the right area, now needs to act on it | **Continue** (send_worker_message) |
| Worker's context is noisy or the approach was wrong | **Spawn fresh** (spawn_worker) |
| Correcting a failure | **Continue** — worker has the error context |
| Verifying another worker's output | **Spawn fresh** — fresh eyes, no assumptions |

## Handling failures

When a worker reports failure, continue it with send_worker_message — it has
the full error context. If correction fails, try a different approach or
report to the user.
"""

TOOL_USE_ENFORCEMENT_GUIDANCE: str = (
    "# Tool-use enforcement\n"
    "You MUST use your tools to take action — do not describe what you would do "
    "or plan to do without actually doing it. When you say you will perform an "
    "action (e.g. 'I will run the tests', 'Let me check the file', 'I will create "
    "the project'), you MUST immediately make the corresponding tool call in the same "
    "response. Never end your turn with a promise of future action — execute it now.\n"
    "Keep working until the task is actually complete. Do not stop with a summary of "
    "what you plan to do next time. If you have tools available that can accomplish "
    "the task, use them instead of telling the user what you would do.\n"
    "Every response should either (a) contain tool calls that make progress, or "
    "(b) deliver a final result to the user. Responses that only describe intentions "
    "without acting are not acceptable."
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
        # Cached rendered "# Available Sub-Agents" block.  The catalog
        # is immutable per builder and every description flows through
        # the regex injection scanner, so rendering once and reusing
        # across turns saves N×turns regex scans per wake cycle.
        self._available_agents_section_cache: str | None = None

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
            parts.append(MEMORY_GUIDANCE)
        if "session_search" in self._available_tools:
            parts.append(SESSION_SEARCH_GUIDANCE)
        if "skill_manage" in self._available_tools:
            parts.append(SKILLS_GUIDANCE)
        if "consult_expert" in self._available_tools:
            parts.append(EXPERT_GUIDANCE)
        if "create_artifact" in self._available_tools:
            parts.append(ARTIFACT_GUIDANCE)

        # Coordinator guidance — injected when the session is in coordinator mode.
        if (
            self._session is not None
            and self._session.config.get("coordinator")
        ):
            parts.append(COORDINATOR_GUIDANCE)

        # Tool-use enforcement for models that tend to skip tools.
        if self._available_tools:
            model_lower = self._get_model_id().lower()
            if any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS):
                parts.append(TOOL_USE_ENFORCEMENT_GUIDANCE)

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
            (
                "You are a helpful, precise, and thorough AI assistant. "
                "Follow the user's instructions carefully. When using tools, "
                "verify results before reporting them."
            ),
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
        # stay within the workspace.
        workspace = self._get_workspace_path()
        if workspace:
            parts.append(f"- **Workspace**: `$HOME` (your working directory)")
            parts.append(
                "\n## Workspace Rules (MANDATORY)\n"
                "Your working directory is `$HOME`. The filesystem is sandboxed — "
                "ALL writes outside `$HOME` are blocked and will fail with "
                "`Read-only file system`.\n\n"
                "**You MUST follow these rules for every command and file operation:**\n"
                "1. ALWAYS work in `$HOME`. Never `cd` to `/tmp`, `/home`, or any absolute path.\n"
                "2. Clone repos with `git clone <url>` (clones into `$HOME/<repo>`) — "
                "NEVER specify an absolute target path.\n"
                "3. Use relative paths for all tools: `read_file`, `write_file`, "
                "`list_files`, `search_files`, `patch`.\n"
                "4. In terminal commands, use relative paths or `$HOME`: "
                "`cd surogate && ls` NOT `cd /home/user/surogate`.\n"
                "5. `/tmp`, `/etc`, `/home`, `/var` are all read-only. "
                "Do not try to write there.\n"
                "6. Never read `~/.ssh`, `~/.aws`, `~/.kube`, or credential files.\n\n"
                "Commands that violate these rules will fail. Do not retry with "
                "a different absolute path — use a relative path instead."
            )

        # Platform hint based on session channel.
        channel = self._get_channel()
        if channel:
            hint = PLATFORM_HINTS.get(channel)
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

        Note: the generic TOOL_USE_ENFORCEMENT_GUIDANCE is injected by
        ``_tool_guidance_section`` (conditional on available tools). This
        method adds the *provider-specific* addenda only.
        """
        if not model_id:
            return ""

        model_lower = model_id.lower()
        parts: list[str] = []

        # OpenAI-specific execution discipline.
        if any(p in model_lower for p in ("gpt", "codex", "o3", "o4")):
            parts.append(OPENAI_MODEL_EXECUTION_GUIDANCE)

        # Google-specific operational guidance.
        if any(p in model_lower for p in ("gemini", "gemma")):
            parts.append(GOOGLE_MODEL_OPERATIONAL_GUIDANCE)

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
