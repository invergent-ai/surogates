"""Live-LLM tests that verify the assembled system prompt and platform skills
produce the intended behaviour.

These tests hit the real LLM configured in ``/work/surogates/config.dev.yaml``
(OpenRouter) so they cost a small amount per run.  They are marked
``@pytest.mark.live`` and excluded from the default ``pytest`` run.  Invoke
explicitly::

    pytest -m live tests/test_system_prompt_live.py -s

The module skips wholesale when the config file is missing or carries no
LLM credentials, so unconfigured developer machines remain green.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import yaml

from surogates.harness.prompt import PromptBuilder
from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader

pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Config + LLM client
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path("/work/surogates/config.dev.yaml")
_PLATFORM_SKILLS_DIR = "/work/surogates/skills"


def _load_llm_config() -> dict[str, str] | None:
    """Read the dev config and return the ``llm`` block, or ``None``.

    Returns ``None`` when the config file is missing, malformed, or lacks
    an api_key / base_url / model — any of which makes the live tests
    unrunnable on this machine.
    """
    if not _CONFIG_PATH.is_file():
        return None
    try:
        raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    llm = (raw or {}).get("llm") or {}
    required = ("model", "base_url", "api_key")
    if not all(llm.get(k) for k in required):
        return None
    return {k: llm[k] for k in required}


_LLM_CONFIG = _load_llm_config()
if _LLM_CONFIG is None:  # pragma: no cover - environment-dependent skip
    pytest.skip(
        f"Live LLM tests need {_CONFIG_PATH} with llm.model / base_url / api_key",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
async def llm_client():
    """Module-scoped :class:`AsyncOpenAI` pointed at the configured base URL.

    Async fixture so the close runs inside pytest-asyncio's managed loop
    rather than against an already-closed default loop at module teardown.
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=_LLM_CONFIG["base_url"],
        api_key=_LLM_CONFIG["api_key"],
    )
    try:
        yield client
    finally:
        # pytest-asyncio gives each test its own loop with auto mode, so a
        # module-scoped fixture's close can race a loop already torn down.
        # The connection is process-bound and will be reclaimed at exit;
        # swallow the cleanup error to keep the run report clean without
        # masking real test failures (which always happen before teardown).
        try:
            await client.close()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Tool schemas the LLM sees + recorder that fakes their results
# ---------------------------------------------------------------------------


def _tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-compatible function-tool schemas for the live LLM.

    Mirrors the surogates built-in tool surface that the prompt guidance
    fragments reference (skill_view, skills_list, delegate_task,
    ask_user_question, read_file, write_file).  Descriptions are kept
    short to leave room for the system prompt.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "skills_list",
                "description": "List available skills with their names and descriptions.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "skill_view",
                "description": "Load the full SKILL.md content of a skill by name.",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": (
                    "Dispatch a focused sub-agent to handle a self-contained task. "
                    "Returns the sub-agent's final response."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "context": {"type": "string"},
                    },
                    "required": ["goal"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ask_user_question",
                "description": "Ask the user a question and wait for their answer.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write content to a file in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_artifact",
                "description": (
                    "Render an inline artifact in the chat — chart, table, "
                    "markdown, html, or svg. Use for visual output the user "
                    "will read in chat, not for files belonging in a project."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["chart", "table", "markdown", "html", "svg"],
                        },
                        "spec": {"type": "object"},
                    },
                    "required": ["name", "kind", "spec"],
                },
            },
        },
    ]


class ToolRecorder:
    """Records LLM tool calls and returns realistic fake results.

    ``skill_view`` returns the real on-disk SKILL.md body so the agent
    can act on what the skill actually says.  The other tools return
    no-op acknowledgements so the conversation can progress without
    materially affecting the world.
    """

    def __init__(self, skills_by_name: dict[str, Any]) -> None:
        self._skills = skills_by_name
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handle(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        if name == "skill_view":
            skill_name = (args.get("name") or "").strip()
            skill = self._skills.get(skill_name)
            if skill is None:
                return f"Skill not found: {skill_name!r}"
            return skill.content or "(empty)"
        if name == "skills_list":
            return json.dumps([
                {"name": s.name, "description": s.description or ""}
                for s in self._skills.values()
            ])
        if name == "delegate_task":
            goal = args.get("goal", "<no goal>")
            return f"[Sub-agent completed] goal={goal!r} — fake result for test."
        if name == "ask_user_question":
            return "User answered: yes, proceed with the recommended approach."
        if name == "read_file":
            return "(fake file content for test)"
        if name == "write_file":
            return "OK (write_file is a no-op in tests)"
        if name == "create_artifact":
            return json.dumps({
                "success": True,
                "artifact_id": "fake-artifact-id",
                "name": args.get("name", ""),
                "kind": args.get("kind", ""),
            })
        return "(unknown tool — recorded only)"

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def calls_for(self, name: str) -> list[dict[str, Any]]:
        return [args for n, args in self.calls if n == name]


# ---------------------------------------------------------------------------
# Tenant + session helpers, mirroring tests/test_platform_hints.py
# ---------------------------------------------------------------------------


def _make_tenant(asset_root: Path, default_model: str) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={
            "agent_name": "Surogate",
            "personality": "You are a careful, terse, helpful assistant.",
            "default_model": default_model,
        },
        user_preferences={},
        permissions=frozenset({"read", "write"}),
        asset_root=str(asset_root),
    )


def _make_session(workspace_path: str | None = None):
    session = MagicMock()
    session.channel = "cli"
    session.model = _LLM_CONFIG["model"]
    session.config = {}
    if workspace_path:
        session.config["workspace_path"] = workspace_path
    return session


# ---------------------------------------------------------------------------
# Module-scoped skill catalog + system-prompt builder
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def platform_skills(tmp_path_factory) -> list[Any]:
    """Load the real platform skills from /work/surogates/skills/ once."""
    asset_root = tmp_path_factory.mktemp("asset_root")
    tenant = _make_tenant(asset_root, _LLM_CONFIG["model"])
    loader = ResourceLoader(platform_skills_dir=_PLATFORM_SKILLS_DIR)
    # Use a fresh loop for the one-shot setup call so we don't depend on
    # any current-loop policy. ``asyncio.run`` creates and closes its own.
    skills = asyncio.run(loader.load_skills(tenant))
    if not skills:
        pytest.skip(
            f"No platform skills loaded from {_PLATFORM_SKILLS_DIR} — "
            "check that the directory exists and contains SKILL.md files."
        )
    return skills


def _build_system_prompt(
    skills: list[Any],
    tmp_path: Path,
    available_tools: set[str],
) -> str:
    tenant = _make_tenant(tmp_path, _LLM_CONFIG["model"])
    session = _make_session(workspace_path=str(tmp_path))
    builder = PromptBuilder(
        tenant,
        skills=skills,
        session=session,
        available_tools=available_tools,
    )
    return builder.build()


# ---------------------------------------------------------------------------
# Multi-turn LLM driver
# ---------------------------------------------------------------------------

# Tools registered with PromptBuilder so the prompt picks up matching guidance.
_DEFAULT_TOOLS = frozenset({
    "skill_view",
    "skill_manage",
    "skills_list",
    "delegate_task",
    "ask_user_question",
    "read_file",
    "write_file",
    "create_artifact",
})


async def _run_turn_loop(
    client,
    system_prompt: str,
    user_message: str,
    recorder: ToolRecorder,
    max_turns: int = 4,
    temperature: float = 0.2,
) -> tuple[str, list[dict[str, Any]]]:
    """Drive a multi-turn conversation until the LLM stops calling tools.

    Returns ``(final_assistant_text, full_message_history)``.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    tools = _tool_schemas()
    final_text = ""
    for _turn in range(max_turns):
        resp = None
        # One quick retry covers transient empty-choices / 5xx from the
        # upstream provider without burning the budget on a flake.
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                resp = await client.chat.completions.create(
                    model=_LLM_CONFIG["model"],
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=1200,
                )
                if resp.choices:
                    break
            except Exception as exc:  # pragma: no cover - network-dependent
                last_err = exc
                resp = None
                await asyncio.sleep(0.5)
        if resp is None or not resp.choices:
            raise RuntimeError(
                f"LLM returned no choices after retry "
                f"(last error: {last_err!r}, model={_LLM_CONFIG['model']!r}). "
                "Possible upstream issue."
            )
        choice = resp.choices[0]
        msg = choice.message
        final_text = msg.content or ""
        assistant_entry: dict[str, Any] = {
            "role": "assistant",
            "content": final_text,
        }
        tool_calls = msg.tool_calls or []
        if tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_entry)

        if not tool_calls:
            break

        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = recorder.handle(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
    return final_text, messages


def _diag(test_name: str, recorder: ToolRecorder, final_text: str) -> str:
    """Build a helpful failure message showing what the LLM actually did."""
    calls_summary = ", ".join(recorder.call_names()) or "<none>"
    snippet = (final_text or "<empty>")[:600]
    return (
        f"[{test_name}]\n"
        f"  recorded tool calls: {calls_summary}\n"
        f"  final assistant text (first 600 chars):\n    {snippet}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_brainstorming_gate_fires_on_creative_work(
    llm_client, platform_skills, tmp_path,
):
    """A 'let's build X' message must NOT produce code immediately.

    The brainstorming gate (system prompt) plus the brainstorming skill
    (in the available-skills index) should cause the LLM to either:
      (a) call ``skill_view("brainstorming")`` to load the design loop, OR
      (b) ask clarifying questions / propose a design without writing code.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "Let's build a small todo-list web app. Single user, no auth.",
        recorder,
    )

    loaded_brainstorm = any(
        args.get("name") == "brainstorming"
        for args in recorder.calls_for("skill_view")
    )
    # Heuristic: a code block in the final text means the agent jumped
    # straight to implementation without a design pass.
    has_code_block = bool(re.search(r"```[a-zA-Z]*\n", final_text or ""))
    asked_question = "?" in (final_text or "")

    assert loaded_brainstorm or (asked_question and not has_code_block), (
        "Expected the agent to load `brainstorming` OR ask design questions "
        "without writing code.\n" + _diag("brainstorming_gate", recorder, final_text)
    )


async def test_simple_definitional_question_skips_brainstorming(
    llm_client, platform_skills, tmp_path,
):
    """A pure definition question should not trigger the brainstorming gate.

    The gate's "Exception" clause covers questions — load no creative skill,
    just answer.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "What does YAGNI stand for?",
        recorder,
    )

    brainstorm_loaded = any(
        args.get("name") == "brainstorming"
        for args in recorder.calls_for("skill_view")
    )
    assert not brainstorm_loaded, (
        "A definitional question must NOT trigger brainstorming.\n"
        + _diag("simple_question", recorder, final_text)
    )

    yagni_phrases = (
        r"you\s*aren'?t\s*gonna\s*need\s*it",
        r"you\s*ain'?t\s*gonna\s*need\s*it",
        r"yagni",
    )
    text_lower = (final_text or "").lower()
    assert any(re.search(p, text_lower) for p in yagni_phrases), (
        "Expected the YAGNI definition in the final reply.\n"
        + _diag("simple_question", recorder, final_text)
    )


async def test_dispatching_parallel_agents_for_independent_failures(
    llm_client, platform_skills, tmp_path,
):
    """Three unrelated investigation tasks should trigger parallel dispatch.

    Either the LLM loads the ``dispatching-parallel-agents`` skill, or it
    proceeds to fan out at least two ``delegate_task`` calls in a single
    turn (the pattern that skill teaches).
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "I have three unrelated failing test files after a refactor:\n"
        "  1. tests/test_auth_abort.py — abort logic, timing issues\n"
        "  2. tests/test_batch_completion.py — tools not executing\n"
        "  3. tests/test_race_conditions.py — execution_count stays at 0\n"
        "They look independent. Please investigate them."
    )
    _, messages = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=3,
    )

    loaded_dispatching = any(
        args.get("name") == "dispatching-parallel-agents"
        for args in recorder.calls_for("skill_view")
    )
    parallel_dispatch = _has_parallel_delegate_task(messages)

    assert loaded_dispatching or parallel_dispatch, (
        "Expected the agent to load `dispatching-parallel-agents` OR issue "
        "at least 2 `delegate_task` calls in a single turn.\n"
        + _diag("dispatch_parallel", recorder, "")
    )


def _has_parallel_delegate_task(messages: list[dict[str, Any]]) -> bool:
    """Return True if any single assistant turn issued ≥2 delegate_task calls."""
    for entry in messages:
        if entry.get("role") != "assistant":
            continue
        tool_calls = entry.get("tool_calls") or []
        delegate_count = sum(
            1 for tc in tool_calls
            if tc.get("function", {}).get("name") == "delegate_task"
        )
        if delegate_count >= 2:
            return True
    return False


async def test_writing_plans_after_approved_design(
    llm_client, platform_skills, tmp_path,
):
    """An approved-design handoff should load the ``writing-plans`` skill."""
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "Design approved. Spec lives at `.surogate/specs/2026-05-27-todo-app.md`. "
        "Architecture: single FastAPI service + SQLite, no auth, one HTML page. "
        "Please create the implementation plan now."
    )
    final_text, _ = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=3,
    )

    loaded_plans = any(
        args.get("name") == "writing-plans"
        for args in recorder.calls_for("skill_view")
    )
    assert loaded_plans, (
        "Expected the agent to load `writing-plans` when handed an approved "
        "design and asked for a plan.\n"
        + _diag("writing_plans", recorder, final_text)
    )


def test_skill_view_recorder_returns_real_body(platform_skills):
    """LLM-free unit test: the recorder hands back the actual on-disk SKILL.md.

    Catches the case where ``ResourceLoader`` failed to populate ``content``
    on the SkillDef and the LLM would see empty skill bodies under load.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    body = recorder.handle("skill_view", {"name": "brainstorming"})
    assert "HARD-GATE" in body, (
        "Brainstorming SKILL.md should include the <HARD-GATE> marker. "
        f"Got (first 200 chars): {body[:200]!r}"
    )
    plans = recorder.handle("skill_view", {"name": "writing-plans"})
    assert "Implementation Plan" in plans, (
        "writing-plans SKILL.md should mention 'Implementation Plan'. "
        f"Got (first 200 chars): {plans[:200]!r}"
    )


async def test_user_override_skips_skill_loading(
    llm_client, platform_skills, tmp_path,
):
    """User's explicit "skip skills" instruction beats the 1% rule.

    The skills guidance documents an instruction priority where the
    user's explicit direction is highest.  A direct user override should
    suppress skill loading for trivial questions even when the prompt
    nominally encourages it.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "Please skip skill loading for this turn — just answer directly. "
        "What is 17 + 25?",
        recorder,
        max_turns=2,
    )

    skill_view_calls = recorder.calls_for("skill_view")
    assert not skill_view_calls, (
        "Explicit 'skip skills' user instruction was ignored — recorded "
        f"skill_view calls: {skill_view_calls}\n"
        + _diag("user_override", recorder, final_text)
    )
    assert "42" in (final_text or ""), (
        "Expected '42' in the final answer.\n"
        + _diag("user_override", recorder, final_text)
    )


# ---------------------------------------------------------------------------
# Artifact tests — exercises guidance/artifact.md and the create_artifact tool
# ---------------------------------------------------------------------------


async def test_artifact_chart_for_numeric_visual_request(
    llm_client, platform_skills, tmp_path,
):
    """A "show me a pie chart of these numbers" request should produce a chart artifact.

    Verifies guidance/artifact.md's "Visual output the user reads as a result"
    bullet and the spec.chart_js nesting rule.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "Show me a pie chart of our quarterly revenue split: "
        "Q1=120, Q2=140, Q3=110, Q4=160 (thousands of USD).",
        recorder,
        max_turns=3,
    )

    chart_calls = [
        args for args in recorder.calls_for("create_artifact")
        if args.get("kind") == "chart"
    ]
    assert chart_calls, (
        "Expected a `create_artifact` call with kind='chart'.\n"
        + _diag("artifact_chart", recorder, final_text)
    )
    # The spec.chart_js nesting rule: the chart config must be under spec.
    spec = chart_calls[0].get("spec") or {}
    assert "chart_js" in spec, (
        "Chart config must live under spec.chart_js, not at the top level. "
        f"Got args: {chart_calls[0]!r}"
    )


async def test_artifact_html_for_self_contained_widget_request(
    llm_client, platform_skills, tmp_path,
):
    """A "make me a small calculator widget" request should be an artifact, not write_file.

    Verifies the disambiguation in guidance/artifact.md — single-file
    HTML demos belong in the chat, not the codebase.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "Build a tiny single-file HTML calculator widget I can play with. "
        "Just addition and subtraction, two inputs and a result.",
        recorder,
        max_turns=3,
    )

    html_artifacts = [
        args for args in recorder.calls_for("create_artifact")
        if args.get("kind") == "html"
    ]
    write_calls = recorder.calls_for("write_file")

    assert html_artifacts, (
        "Expected an HTML `create_artifact` call for the inline widget.\n"
        + _diag("artifact_html", recorder, final_text)
    )
    assert not write_calls, (
        "Single-file widget belongs in an artifact, not on disk. "
        f"Got unexpected write_file calls: {write_calls}\n"
        + _diag("artifact_html", recorder, final_text)
    )


async def test_write_file_for_codebase_file_not_artifact(
    llm_client, platform_skills, tmp_path,
):
    """A "save this to my project" request should go to write_file, not create_artifact.

    Negative case for the artifact disambiguation: when the output's home
    is a codebase, the agent must NOT bounce it through create_artifact.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "Save a Python module to `src/utils/slugify.py` that exposes a "
        "`slugify(text: str) -> str` function (lowercase, hyphens, ASCII-only). "
        "This file goes into our codebase.",
        recorder,
        max_turns=3,
    )

    write_calls = recorder.calls_for("write_file")
    artifact_calls = recorder.calls_for("create_artifact")

    assert write_calls, (
        "Codebase file should land via write_file.\n"
        + _diag("write_file_disambig", recorder, final_text)
    )
    assert not artifact_calls, (
        "Codebase file must NOT go through create_artifact. "
        f"Got: {artifact_calls}\n"
        + _diag("write_file_disambig", recorder, final_text)
    )


# ---------------------------------------------------------------------------
# Planning depth tests
# ---------------------------------------------------------------------------


async def test_writing_plans_produces_concrete_tasks_with_paths(
    llm_client, platform_skills, tmp_path,
):
    """After loading writing-plans, the agent's draft must use exact paths + numbered steps.

    The writing-plans skill's "No Placeholders" section forbids vague tasks.
    A drafted plan should mention specific file paths and at least one
    numbered step or checkbox. Spec content is embedded directly in the
    user message so the agent doesn't need to read a file — that keeps the
    response budget focused on the plan itself.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "Design already approved. Here is the full spec inline (no file read needed):\n\n"
        "---\n"
        "Goal: convert a CSV file to a dict keyed by the first column.\n"
        "Architecture: single Python module `src/csv2json.py` with a "
        "`convert(path: str) -> dict` function; tests live at "
        "`tests/test_csv2json.py`; one CLI entry at `bin/csv2json.py`.\n"
        "Tech Stack: Python 3.12 stdlib only, pytest.\n"
        "---\n\n"
        "Please draft the implementation plan now — show it inline. "
        "Just one short bite-sized task is enough for me to review the format."
    )
    final_text, _ = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=4,
    )

    loaded_plans = any(
        args.get("name") == "writing-plans"
        for args in recorder.calls_for("skill_view")
    )
    assert loaded_plans, (
        "Expected writing-plans to be loaded before drafting.\n"
        + _diag("plans_concrete", recorder, final_text)
    )

    text = final_text or ""
    has_real_path = bool(
        re.search(r"src/csv2json\.py|tests/test_csv2json\.py|bin/csv2json\.py", text)
    )
    has_step_marker = bool(re.search(r"- \[ \]|Step \d|Task \d", text))

    assert has_real_path and has_step_marker, (
        "Drafted plan should mention the exact spec'd file paths AND use "
        "checkbox/Step/Task markers.\n"
        + _diag("plans_concrete", recorder, final_text)
    )


async def test_executing_plans_loads_on_inline_execution_request(
    llm_client, platform_skills, tmp_path,
):
    """An "execute this plan inline" request should load executing-plans."""
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "I have an implementation plan at `.surogate/plans/2026-05-27-feature.md` "
        "with 5 tasks. Please execute it inline — no sub-agents — and check "
        "in with me between tasks."
    )
    final_text, _ = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=3,
    )

    loaded_exec = any(
        args.get("name") == "executing-plans"
        for args in recorder.calls_for("skill_view")
    )
    assert loaded_exec, (
        "Expected executing-plans to be loaded for inline execution.\n"
        + _diag("execute_inline", recorder, final_text)
    )


# ---------------------------------------------------------------------------
# Delegation tests
# ---------------------------------------------------------------------------


async def test_single_delegate_task_for_focused_investigation(
    llm_client, platform_skills, tmp_path,
):
    """A single focused investigation should use ONE delegate_task, not fan out.

    Negative case for dispatching-parallel-agents: don't fan out when
    there's only one problem.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "Please investigate why `compute_total(items)` in `src/billing.py` "
        "returns None for empty lists when it should return 0. Look at the "
        "function, identify the bug, and propose a one-line fix."
    )
    final_text, messages = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=3,
    )

    # If the model delegated, it should only have dispatched ONE worker;
    # equally valid is the model doing it directly (no delegate_task at all).
    parallel = _has_parallel_delegate_task(messages)
    assert not parallel, (
        "Single focused investigation should not fan out into parallel "
        "delegate_task calls.\n"
        + _diag("single_delegate", recorder, final_text)
    )


async def test_parallel_delegate_task_for_unrelated_refactors(
    llm_client, platform_skills, tmp_path,
):
    """Three unrelated refactors should fan out via parallel delegate_task.

    This is dispatching-parallel-agents in the "edit independent files"
    case rather than the "investigate failures" case.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "I need three independent refactors done. They touch unrelated files "
        "and shouldn't conflict:\n"
        "  1. `src/auth/login.py` — extract the password-hashing helper into a "
        "module-level function.\n"
        "  2. `src/billing/invoice.py` — rename `calc_total` to `compute_total` "
        "everywhere in this file.\n"
        "  3. `src/notifications/email.py` — replace the inline SMTP timeout "
        "constant with a config-driven value.\n"
        "Please dispatch sub-agents for each."
    )
    _, messages = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=3,
    )

    parallel = _has_parallel_delegate_task(messages)
    # As an alternate signal, accept ≥2 delegate_task calls overall even
    # if the model serialised them — the skill's preference is parallel,
    # but the prompt allows judgment when context says serialise.
    total_delegates = len(recorder.calls_for("delegate_task"))

    assert parallel or total_delegates >= 2, (
        "Expected at least 2 delegate_task calls (parallel preferred) for "
        f"three independent refactors. Got {total_delegates} total.\n"
        + _diag("parallel_refactor", recorder, "")
    )


# ---------------------------------------------------------------------------
# Skill discovery + creation tests
# ---------------------------------------------------------------------------


async def test_writing_skills_loads_on_skill_creation_request(
    llm_client, platform_skills, tmp_path,
):
    """A "create a new skill for X" request should load writing-skills."""
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    user_msg = (
        "I want to create a new skill that captures our approach to migrating "
        "PostgreSQL schemas safely (online, no downtime). Walk me through "
        "creating that skill."
    )
    final_text, _ = await _run_turn_loop(
        llm_client, system_prompt, user_msg, recorder, max_turns=3,
    )

    loaded_writing_skills = any(
        args.get("name") == "writing-skills"
        for args in recorder.calls_for("skill_view")
    )
    assert loaded_writing_skills, (
        "Expected writing-skills to be loaded when asked to author a new skill.\n"
        + _diag("writing_skills", recorder, final_text)
    )


async def test_skills_list_or_named_cite_when_asked_about_capabilities(
    llm_client, platform_skills, tmp_path,
):
    """An "what skills are available" request should produce concrete names.

    Either the LLM calls skills_list to enumerate, or it answers from the
    in-prompt skill index citing specific names. A vague answer without
    naming any platform skill counts as a failure.
    """
    recorder = ToolRecorder({s.name: s for s in platform_skills})
    system_prompt = _build_system_prompt(platform_skills, tmp_path, _DEFAULT_TOOLS)

    final_text, _ = await _run_turn_loop(
        llm_client,
        system_prompt,
        "What skills do you have available for software development tasks? "
        "I want to know what specialized workflows you can apply.",
        recorder,
        max_turns=2,
    )

    called_skills_list = bool(recorder.calls_for("skills_list"))
    text_lower = (final_text or "").lower()
    cited_names = [
        name for name in (
            "brainstorming", "writing-plans", "executing-plans",
            "writing-skills", "dispatching-parallel-agents",
            "test-driven-development", "systematic-debugging",
            "subagent-driven-development", "requesting-code-review",
        )
        if name in text_lower
    ]

    assert called_skills_list or len(cited_names) >= 2, (
        "Expected the agent to call skills_list OR cite ≥2 specific skill "
        f"names from the index. Cited: {cited_names}\n"
        + _diag("capabilities", recorder, final_text)
    )
