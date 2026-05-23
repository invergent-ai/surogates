# Expert Mechanism Resurrection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing-but-unreachable expert consultation runtime into the harness so the base LLM can call `consult_expert` and users can invoke `/<expert>` slash commands; render an `# Available Experts` section in the system prompt; excise the dead auto-router and forced-route compat shims; remove the unreachable auto-disable code path.

**Architecture:** Voluntary consultation only — no auto-routing, no auto-disable. Two entry points (slash command, `consult_expert` tool) converge on the existing `ExpertConsultationService.consult()`. The hidden advisor and hard-task classifier are untouched.

**Tech Stack:** Python 3.12, async SQLAlchemy, FastAPI, OpenAI-compatible chat completions (`openai.AsyncOpenAI`), `pytest`. Project repo: `/work/surogates`. Plans/specs in `docs/superpowers/`.

**Spec:** [`docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md`](../specs/2026-05-23-expert-mechanism-resurrection-design.md)

---

## Task Status

Updated before every commit so the plan is the single source of truth for progress.

| # | Task | Status |
|---|---|---|
| 1 | Branch and orient | completed |
| 2 | Register `consult_expert` in the tool runtime | completed |
| 3 | Update the `consult_expert` schema description | completed |
| 4 | Surface expert metadata through `skills_list` | completed |
| 5 | Render `# Available Experts` in the system prompt | completed |
| 6 | Simplify `record_expert_outcome` + reorder delegation emit | completed |
| 7 | Expert branch in `expand_slash_skill` | completed |
| 8 | Wire the new `expand_slash_skill` shape into the harness loop | completed |
| 9 | Excise dead auto-router and forced-route shims | in progress |
| 10 | Documentation updates | pending |
| 11 | Final verification | pending |

---

## File map

Each file below is touched by at least one task. Read this before starting Task 1 to internalize the surface area.

**Modify (production code):**
- `surogates/tools/runtime.py` — add `expert` to imports and `modules` list so `register()` runs at startup.
- `surogates/tools/router.py` — add `"consult_expert": ToolLocation.HARNESS` to `TOOL_LOCATIONS`.
- `surogates/tools/builtin/expert.py` — change schema description; remove "delegate"/"subtask" wording.
- `surogates/tools/builtin/skills.py` — extend `_skills_list_handler` entries to include `type`, `trigger`, `expert_status`, `expert_model`, `expert_endpoint`; append one sentence to `SKILLS_LIST_SCHEMA.description`.
- `surogates/tools/builtin/expert_feedback.py` — gut `_update_db_stats`; drop `AUTO_DISABLE_THRESHOLD`, `MIN_USES_FOR_AUTO_DISABLE`; drop `db_session` / `skill_id` parameters from `record_expert_outcome`.
- `surogates/tools/builtin/expert_service.py` — emit `expert.delegation` before endpoint validation, so even missing-endpoint failures join correctly in `v_expert_outcomes`; verify the two `record_expert_outcome` call sites still pass no removed kwargs.
- `surogates/harness/slash_skill.py` — add expert branch before `skill_view` dispatch; widen `expand_slash_skill` to accept `session_store` and `sandbox_pool`; widen return tuple with a `kind: Literal["skill", "expert"]` discriminator.
- `surogates/harness/loop.py` — at the `expand_slash_skill` call site, pass `session_store=self._store` and `sandbox_pool=self._sandbox_pool`; consume the new 4-tuple; only emit `SKILL_INVOKED` when `kind == "skill"`; delete `_has_forced_expert_after_latest_user`, `_forced_expert_categories_after_latest_user`, `_legacy_forced_expert_categories_after_latest_user`.
- `surogates/harness/expert_routing.py` — delete `select_expert_for_task`, `load_skills_for_expert_routing`, `classify_tool_calls`, `_TRIGGER_SPLIT_RE`, `_WORD_RE`, `_TRIGGER_STOPWORDS`, `_normalise_trigger_text`, `_trigger_match_score`.
- `surogates/harness/prompt.py` — add `_available_experts_section` method; call it from `build()`.

**Modify (tests):**
- `tests/test_expert.py` — flip the existing negative tests (`test_consult_expert_not_in_tool_locations`, `test_expert_not_registered`, `test_expert_guidance_not_injected_*`, `test_skills_section_hides_experts_*` where they cross the new contract); add new positive assertions.
- `tests/test_expert_routing.py` — drop the tests for the deleted helpers; keep tests for `classify_hard_task`, the regex fallback, and any thinking-toggle helpers.

**Modify (docs):**
- `docs/experts/index.md` — four edits per the spec.

**Create:**
- `tests/test_slash_expert.py` — new file covering the slash-command expert branch.

---

## Task 1: Branch and orient

**Files:**
- Read-only: `docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md`

- [ ] **Step 1.1: Verify clean working tree**

```bash
cd /work/surogates && git status
```

Expected: no unrelated uncommitted changes. If the design spec or this plan
is still modified locally, commit or stash those documentation changes before
starting the implementation branch.

- [ ] **Step 1.2: Create implementation branch**

```bash
cd /work/surogates && git checkout -b feat/expert-resurrection
```

Expected: `Switched to a new branch 'feat/expert-resurrection'`.

- [ ] **Step 1.3: Confirm the test runner works**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py -q --no-header 2>&1 | tail -20
```

Expected: all current tests pass (these tests assert *current* behavior; we will flip the contradicting ones in later tasks).

---

## Task 2: Register `consult_expert` in the tool runtime

**Why first:** Cheapest change with the most leverage — once registered, the rest of the chain (handler → service → mini-loop) becomes reachable.

**Files:**
- Modify: `surogates/tools/runtime.py:50-90`
- Modify: `surogates/tools/router.py:44-101` (add one line)
- Test: `tests/test_expert.py` (flip two negative tests)

- [ ] **Step 2.1: Flip the "expert not registered" test to assert registered**

In `tests/test_expert.py` around line 875, replace:

```python
class TestToolRuntimeRegistersExpert:
    """ToolRuntime.register_builtins() hides consult_expert from executors."""

    def test_expert_not_registered(self):
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        reg = ToolRegistry()
        runtime = ToolRuntime(reg)
        runtime.register_builtins()
        assert not reg.has("consult_expert")
```

with:

```python
class TestToolRuntimeRegistersExpert:
    """ToolRuntime.register_builtins() exposes consult_expert to executors."""

    def test_expert_registered(self):
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.runtime import ToolRuntime

        reg = ToolRegistry()
        runtime = ToolRuntime(reg)
        runtime.register_builtins()
        assert reg.has("consult_expert")
        entry = reg.get("consult_expert")
        assert entry.schema.name == "consult_expert"
        assert entry.toolset == "expert"
```

- [ ] **Step 2.2: Flip the "consult_expert not in TOOL_LOCATIONS" test**

In `tests/test_expert.py` around line 861, replace:

```python
class TestToolRouterExpertLocation:
    """consult_expert is not exposed through normal tool routing."""

    def test_consult_expert_not_in_tool_locations(self):
        from surogates.tools.router import TOOL_LOCATIONS

        assert "consult_expert" not in TOOL_LOCATIONS
```

with:

```python
class TestToolRouterExpertLocation:
    """consult_expert routes to the harness, not the sandbox."""

    def test_consult_expert_routes_to_harness(self):
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

        assert TOOL_LOCATIONS["consult_expert"] is ToolLocation.HARNESS

    def test_consult_expert_resolves_to_harness_via_router(self):
        from unittest.mock import MagicMock
        from surogates.tools.router import ToolLocation, ToolRouter

        router = ToolRouter(
            registry=MagicMock(),
            sandbox_pool=MagicMock(),
            governance=MagicMock(),
        )
        assert router.resolve_location("consult_expert") is ToolLocation.HARNESS
```

- [ ] **Step 2.3: Run the flipped tests; verify they FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestToolRuntimeRegistersExpert tests/test_expert.py::TestToolRouterExpertLocation -v
```

Expected: all four test methods FAIL — runtime doesn't register the module yet, and `TOOL_LOCATIONS` doesn't contain `consult_expert`.

- [ ] **Step 2.4: Add `expert` to the import group in `surogates/tools/runtime.py`**

Locate the import block at lines 50-68 and add `expert` (alphabetically between `delegate` and `file_ops`). The block becomes:

```python
from surogates.tools.builtin import (
    artifact,
    browser,
    clarify,
    coordinator,
    cron,
    delegate,
    expert,
    file_ops,
    kb_tools,
    loop_control,
    memory,
    session_search,
    skill_manager,
    skills,
    terminal,
    todo,
    vision,
    web_search,
)
```

- [ ] **Step 2.5: Add `expert` to the `modules` list**

In the same file, after the import block (around line 71), the `modules` list becomes:

```python
modules = [
    memory,
    skills,
    skill_manager,
    vision,
    web_search,
    browser,
    file_ops,
    kb_tools,
    loop_control,
    delegate,
    expert,
    terminal,  # also registers the 'process' tool
    session_search,
    todo,
    clarify,
    cron,
    coordinator,
    artifact,
    task_tools,  # spawn_task, unblock_task, cancel_task, task_block
]
```

`expert` goes after `delegate` for the same reason `terminal` follows others — order does not strictly matter for registration but the grouping (deliberative tools together) is clearer.

- [ ] **Step 2.6: Add `consult_expert` to `TOOL_LOCATIONS` in `surogates/tools/router.py`**

In `surogates/tools/router.py` around line 56, after the line `"delegate_task": ToolLocation.HARNESS,`, add:

```python
    "consult_expert": ToolLocation.HARNESS,
```

This places it in the deliberative-tools group, immediately below `delegate_task`.

- [ ] **Step 2.7: Run the flipped tests; verify they PASS**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestToolRuntimeRegistersExpert tests/test_expert.py::TestToolRouterExpertLocation -v
```

Expected: all four pass.

- [ ] **Step 2.8: Commit**

```bash
cd /work/surogates && git add \
  surogates/tools/runtime.py \
  surogates/tools/router.py \
  tests/test_expert.py
cd /work/surogates && git commit -m "feat(experts): register consult_expert and route to harness

The builtin was defined but never imported by ToolRuntime, so the
LLM never saw the schema. Tool location was also missing, so the
router would have tried to dispatch to the sandbox. Wire both up."
```

---

## Task 3: Update the `consult_expert` schema description

**Why:** Existing description uses "delegate"/"subtask" which collides with `delegate_task`. Spec § "Tool schema description".

**Files:**
- Modify: `surogates/tools/builtin/expert.py:28-32`
- Test: `tests/test_expert.py` (add a regression test)

- [ ] **Step 3.1: Add a test asserting the new description shape**

Append to `tests/test_expert.py`:

```python
class TestConsultExpertSchemaDescription:
    """consult_expert description must not collide with delegate_task vocabulary."""

    def test_description_uses_consult_not_delegate(self):
        from surogates.tools.builtin.expert import _EXPERT_SCHEMA

        desc = _EXPERT_SCHEMA.description.lower()
        assert "consult" in desc
        # Must not use the words that belong to delegate_task.
        assert "delegate" not in desc
        assert "subtask" not in desc
        assert "sub-task" not in desc

    def test_description_mentions_specialist_and_specialty(self):
        from surogates.tools.builtin.expert import _EXPERT_SCHEMA

        desc = _EXPERT_SCHEMA.description.lower()
        assert "specialist" in desc
        assert "specialty" in desc
```

- [ ] **Step 3.2: Run the test; verify FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestConsultExpertSchemaDescription -v
```

Expected: both tests FAIL — current description contains "delegate" and "subtask".

- [ ] **Step 3.3: Update the schema description**

In `surogates/tools/builtin/expert.py:28-32`, replace:

```python
    description=(
        "Delegate a subtask to a task-specialized expert model. The "
        "expert handles the subtask and returns its result. Use this "
        "when a task falls within an available expert's specialty."
    ),
```

with:

```python
    description=(
        "Consult a specialist model for a single domain question. The "
        "expert answers and returns its deliverable. Use this when a "
        "request falls within an available expert's specialty."
    ),
```

- [ ] **Step 3.4: Run the test; verify PASS**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestConsultExpertSchemaDescription -v
```

Expected: both pass.

- [ ] **Step 3.5: Run all existing expert tests to make sure nothing else broke**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py -q
```

Expected: only flipped tests changed status; nothing new breaks.

- [ ] **Step 3.6: Commit**

```bash
cd /work/surogates && git add \
  surogates/tools/builtin/expert.py \
  tests/test_expert.py
cd /work/surogates && git commit -m "feat(experts): retire 'delegate' / 'subtask' from consult_expert schema

Vocabulary collision with delegate_task. The new wording uses
'consult' and 'specialist'/'specialty', matching the design spec."
```

---

## Task 4: Surface expert metadata through `skills_list`

**Why:** The LLM needs to recognise `type=expert` entries so it routes to `consult_expert`. Today the handler returns only `name + description + category`.

**Files:**
- Modify: `surogates/tools/builtin/skills.py:59-72` (schema description)
- Modify: `surogates/tools/builtin/skills.py:168-219` (handler)
- Test: `tests/test_expert.py` (new test)

- [ ] **Step 4.1: Write a test for the expanded handler output**

Append to `tests/test_expert.py`:

```python
class TestSkillsListExpertMetadata:
    """skills_list returns enough metadata to identify and address active experts."""

    @pytest.mark.asyncio
    async def test_handler_includes_expert_fields(self, monkeypatch):
        import json
        from types import SimpleNamespace
        from uuid import uuid4

        from surogates.tools.builtin.skills import _skills_list_handler
        from surogates.tools.loader import SkillDef

        active = SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="body",
            source="org",
            type="expert",
            expert_status="active",
            expert_model="qwen2.5-coder-7b",
            expert_endpoint="http://expert:8000/v1",
            trigger="SQL queries, database schemas",
        )
        plain = SkillDef(
            name="code_review",
            description="Reviews code",
            content="body",
            source="org",
            type="skill",
            trigger="code review",
        )

        async def fake_loader(tenant, **kwargs):
            return [active, plain]

        monkeypatch.setattr(
            "surogates.tools.builtin.skills._load_all_skills", fake_loader,
        )

        tenant = SimpleNamespace(org_id=uuid4())
        out = await _skills_list_handler({}, tenant=tenant)
        payload = json.loads(out)

        by_name = {s["name"]: s for s in payload["skills"]}
        assert by_name["sql_writer"]["type"] == "expert"
        assert by_name["sql_writer"]["trigger"] == "SQL queries, database schemas"
        assert by_name["sql_writer"]["expert_status"] == "active"
        assert by_name["sql_writer"]["expert_model"] == "qwen2.5-coder-7b"
        assert by_name["sql_writer"]["expert_endpoint"] == "http://expert:8000/v1"
        assert by_name["code_review"]["type"] == "skill"
        # Regular skills do not get expert_* keys.
        assert "expert_status" not in by_name["code_review"]
        assert "expert_model" not in by_name["code_review"]

    def test_schema_description_directs_to_consult_expert(self):
        from surogates.tools.builtin.skills import SKILLS_LIST_SCHEMA

        desc = SKILLS_LIST_SCHEMA.description
        assert "type: expert" in desc or "type=expert" in desc.replace(": ", "=")
        assert "consult_expert" in desc
```

- [ ] **Step 4.2: Run the test; verify FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestSkillsListExpertMetadata -v
```

Expected: both tests FAIL — current handler returns only `name + description + category`; current schema description does not mention `consult_expert`.

- [ ] **Step 4.3: Extend the handler to include expert metadata**

In `surogates/tools/builtin/skills.py:191-200`, replace:

```python
    skill_list: list[dict[str, Any]] = []
    for s in skills:
        entry: dict[str, Any] = {
            "name": s.name,
            "description": s.description,
            "category": s.category,
        }
        if category_filter and s.category != category_filter:
            continue
        skill_list.append(entry)
```

with:

```python
    skill_list: list[dict[str, Any]] = []
    for s in skills:
        if category_filter and s.category != category_filter:
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
```

The `category_filter` check moves to the top of the loop so we skip the build cost for excluded entries. `getattr` defaults keep the handler robust against dict-shaped inputs and older `SkillDef` instances.

- [ ] **Step 4.4: Update the schema description**

In `surogates/tools/builtin/skills.py:59-72`, replace:

```python
SKILLS_LIST_SCHEMA = ToolSchema(
    name="skills_list",
    description="List available skills (name + description). Use skill_view(name) to load full content.",
    ...
)
```

with:

```python
SKILLS_LIST_SCHEMA = ToolSchema(
    name="skills_list",
    description=(
        "List available skills (name + description). Use skill_view(name) "
        "to load full content. Entries with type: expert are specialist "
        "models; consult active experts via consult_expert(expert, task) "
        "rather than skill_view."
    ),
    ...
)
```

- [ ] **Step 4.5: Run the test; verify PASS**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestSkillsListExpertMetadata -v
```

Expected: both pass.

- [ ] **Step 4.6: Re-run the full skills test file to catch contract drift**

```bash
cd /work/surogates && uv run pytest tests/test_skills_config.py tests/test_skill_manager.py -q
```

Expected: no regressions. (If any test pinned the old 3-field handler shape, that's a contract drift we created — update those assertions to allow the additional fields.)

- [ ] **Step 4.7: Commit**

```bash
cd /work/surogates && git add \
  surogates/tools/builtin/skills.py \
  tests/test_expert.py
cd /work/surogates && git commit -m "feat(experts): expose expert metadata through skills_list

Entries with type=expert now carry expert_status, expert_model,
expert_endpoint, and trigger so the LLM can recognise them and
route via consult_expert. Schema description points the LLM at
consult_expert for those entries."
```

---

## Task 5: Render `# Available Experts` in the system prompt

**Why:** Strongest discoverability signal; sits in every chat completion request.

**Files:**
- Modify: `surogates/harness/prompt.py` (add `_available_experts_section`, call from `build()`)
- Modify: `tests/test_expert.py` (flip the negative tests at lines 716-788)

- [ ] **Step 5.1: Flip the existing negative tests in `tests/test_expert.py`**

Replace the entire `class TestPromptBuilderExpertGuidance` block (lines 703-789 in the current file) with:

```python
class TestPromptBuilderExpertSection:
    """PromptBuilder renders the # Available Experts section for active experts."""

    @pytest.fixture
    def tenant(self):
        from types import SimpleNamespace
        from uuid import uuid4
        return SimpleNamespace(
            org_id=uuid4(),
            user_id=uuid4(),
            org_config={"default_model": "gpt-4o"},
            user_preferences={},
            asset_root="/tmp/test_assets",
        )

    def test_section_empty_when_no_experts(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        pb = PromptBuilder(tenant=tenant, skills=[])
        assert pb._available_experts_section() == ""

    def test_section_lists_active_experts(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="sql_writer",
                description="Writes PostgreSQL queries from natural language descriptions",
                content="body",
                source="org",
                type="expert",
                expert_status="active",
                trigger="SQL queries, database schemas, PostgreSQL, data analysis",
            ),
            SkillDef(
                name="draft_expert",
                description="Not yet active",
                content="body",
                source="org",
                type="expert",
                expert_status="draft",
                trigger="something",
            ),
            SkillDef(
                name="regular_skill",
                description="A normal skill",
                content="body",
                source="org",
                type="skill",
            ),
        ]

        pb = PromptBuilder(tenant=tenant, skills=skills)
        section = pb._available_experts_section()

        assert "# Available Experts" in section
        assert "sql_writer" in section
        assert "Writes PostgreSQL queries" in section
        assert "Specialty: SQL queries, database schemas" in section
        # Only active experts are listed.
        assert "draft_expert" not in section
        # Regular skills do not appear here.
        assert "regular_skill" not in section
        # Section instructs the LLM how to invoke and disambiguates from delegate_task.
        assert "consult_expert(expert, task)" in section
        assert "delegate_task" in section
        assert "Do NOT use" in section

    def test_section_emitted_in_build_when_active_expert_exists(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="sql_writer",
                description="SQL specialist",
                content="body",
                source="org",
                type="expert",
                expert_status="active",
                trigger="SQL queries",
            ),
        ]
        pb = PromptBuilder(tenant=tenant, skills=skills)
        prompt = pb.build()
        assert "# Available Experts" in prompt
        assert "sql_writer" in prompt

    def test_section_omitted_in_build_when_no_active_expert(self, tenant):
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="regular_skill",
                description="A normal skill",
                content="body",
                source="org",
                type="skill",
            ),
        ]
        pb = PromptBuilder(tenant=tenant, skills=skills)
        prompt = pb.build()
        assert "# Available Experts" not in prompt

    def test_skills_section_still_excludes_experts(self, tenant):
        """Regression: experts must not bleed into the regular skills catalog."""
        from surogates.harness.prompt import PromptBuilder

        skills = [
            SkillDef(
                name="sql_writer",
                description="SQL specialist",
                content="body",
                source="org",
                type="expert",
                expert_status="active",
                trigger="SQL queries",
            ),
            SkillDef(
                name="code_review",
                description="Reviews code",
                content="body",
                source="org",
                type="skill",
            ),
        ]
        pb = PromptBuilder(tenant=tenant, skills=skills)
        skills_section = pb._skills_section()
        # sql_writer must NOT appear in the regular skills index — that
        # remains the contract of _skills_section.
        assert "sql_writer" not in skills_section
        assert "code_review" in skills_section
```

The existing `TestWorkerExpertCatalogWiring` class (just below) stays unchanged — it verifies the worker passes the loaded catalog into the builder.

- [ ] **Step 5.2: Run the new tests; verify FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestPromptBuilderExpertSection -v
```

Expected: all five tests FAIL — `_available_experts_section` doesn't exist yet.

- [ ] **Step 5.3: Add `_available_experts_section` to `PromptBuilder`**

In `surogates/harness/prompt.py`, after `_available_agents_section` (around line 362, before `_memory_section`), insert:

```python
    def _available_experts_section(self) -> str:
        """Render the `# Available Experts` system-prompt block.

        Lists active experts (``type=expert`` and ``expert_status="active"``)
        with their description and trigger phrases.  The LLM uses
        ``consult_expert(expert, task)`` to delegate; the block also
        disambiguates from ``delegate_task`` which has a different
        purpose (multi-step sub-agent work in a fresh session).

        Returns an empty string when no active experts are loaded so
        the section is omitted entirely from the prompt.
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
```

- [ ] **Step 5.4: Call `_available_experts_section` from `build()`**

In `surogates/harness/prompt.py:148-182`, the `build()` method's `sections.append(...)` block currently runs:

```python
        sections.append(self._memory_section())
        sections.append(self._skills_section())
        sections.append(self._preloaded_skills_section())
        sections.append(self._available_agents_section())
```

Insert the experts section between `_available_agents_section` and `_kb_section`:

```python
        sections.append(self._memory_section())
        sections.append(self._skills_section())
        sections.append(self._preloaded_skills_section())
        sections.append(self._available_agents_section())
        sections.append(self._available_experts_section())
        sections.append(self._kb_section())
```

Update the docstring layer comment immediately above (`"""Assemble the full system prompt. Layers: ..."`) by adding the line `8. Available experts (when any are active)` between the `Available sub-agents` line and the `Context files` line, and renumber subsequent lines.

- [ ] **Step 5.5: Run the new tests; verify PASS**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestPromptBuilderExpertSection -v
```

Expected: all five pass.

- [ ] **Step 5.6: Run the full expert test suite to make sure nothing else broke**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py -q
```

Expected: only the flipped tests differ from before; no surprises.

- [ ] **Step 5.7: Commit**

```bash
cd /work/surogates && git add \
  surogates/harness/prompt.py \
  tests/test_expert.py
cd /work/surogates && git commit -m "feat(experts): render '# Available Experts' system-prompt section

The LLM now sees active experts (name, description, specialty
phrases) plus a directive to call consult_expert. The block also
disambiguates from delegate_task to avoid vocabulary collision."
```

---

## Task 6: Simplify `record_expert_outcome` (drop auto-disable + DB-stats path)

**Why:** Auto-disable is a non-goal; the unreachable DB path makes `record_expert_outcome` carry parameters its callers don't usefully populate.

**Files:**
- Modify: `surogates/tools/builtin/expert_feedback.py`
- Modify: `surogates/tools/builtin/expert_service.py:90, 178`
- Test: `tests/test_expert.py` (add regression tests for the slim signature and delegation-before-outcome ordering)

- [ ] **Step 6.1: Write the regression test**

Append to `tests/test_expert.py`:

```python
class TestRecordExpertOutcomeSlim:
    """record_expert_outcome only emits events; no DB stat updates."""

    @pytest.mark.asyncio
    async def test_emits_result_event_on_success(self):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from surogates.session.events import EventType
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        store = AsyncMock()
        session_id = uuid4()
        await record_expert_outcome(
            session_store=store,
            session_id=session_id,
            expert_name="sql_writer",
            success=True,
            iterations_used=3,
            content="SELECT 1",
        )
        store.emit_event.assert_awaited_once()
        args, _ = store.emit_event.call_args
        assert args[0] == session_id
        assert args[1] is EventType.EXPERT_RESULT
        assert args[2]["expert"] == "sql_writer"
        assert args[2]["success"] is True
        assert args[2]["iterations_used"] == 3
        assert args[2]["content"] == "SELECT 1"

    @pytest.mark.asyncio
    async def test_emits_failure_event_on_failure(self):
        from unittest.mock import AsyncMock
        from uuid import uuid4
        from surogates.session.events import EventType
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        store = AsyncMock()
        await record_expert_outcome(
            session_store=store,
            session_id=uuid4(),
            expert_name="x",
            success=False,
            error="boom",
        )
        args, _ = store.emit_event.call_args
        assert args[1] is EventType.EXPERT_FAILURE
        assert args[2]["error"] == "boom"

    def test_signature_has_no_db_kwargs(self):
        import inspect
        from surogates.tools.builtin.expert_feedback import record_expert_outcome

        params = inspect.signature(record_expert_outcome).parameters
        assert "db_session" not in params
        assert "skill_id" not in params

    def test_auto_disable_constants_removed(self):
        from surogates.tools.builtin import expert_feedback

        assert not hasattr(expert_feedback, "AUTO_DISABLE_THRESHOLD")
        assert not hasattr(expert_feedback, "MIN_USES_FOR_AUTO_DISABLE")
        assert not hasattr(expert_feedback, "_update_db_stats")
```

- [ ] **Step 6.2: Run the new tests; verify FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestRecordExpertOutcomeSlim -v
```

Expected: `test_signature_has_no_db_kwargs` and `test_auto_disable_constants_removed` FAIL; the event-emission tests may pass depending on existing behavior.

- [ ] **Step 6.3: Rewrite `expert_feedback.py`**

Replace the entire contents of `surogates/tools/builtin/expert_feedback.py` with:

```python
"""Expert outcome event emission.

Emits ``EXPERT_RESULT`` or ``EXPERT_FAILURE`` events for each
consultation so the SQL views, training collector, and feedback API
have a complete trajectory. Auto-disable is intentionally not
implemented — operators retire experts manually via
``POST /v1/skills/{name}/retire``. Quality signals come from
``EXPERT_ENDORSE`` / ``EXPERT_OVERRIDE`` events the feedback API
emits when a user or judge rates an ``expert.result``.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.session.events import EventType

logger = logging.getLogger(__name__)


async def record_expert_outcome(
    *,
    session_store: Any,
    session_id: Any,
    expert_name: str,
    success: bool,
    iterations_used: int = 0,
    error: str | None = None,
    content: str | None = None,
    forced: bool = False,
    category: str | None = None,
) -> None:
    """Emit the outcome event for an expert consultation.

    Parameters
    ----------
    session_store:
        The :class:`~surogates.session.store.SessionStore` for emitting
        events. When ``None`` the function is a no-op.
    session_id:
        The current session UUID.
    expert_name:
        The name of the expert that was consulted.
    success:
        ``True`` if the expert completed without error.
    iterations_used:
        Number of mini-loop iterations the expert consumed.
    error:
        Error message when ``success`` is ``False``.
    content:
        The expert's deliverable text (only present on success).
    forced, category:
        Legacy kwargs preserved for the slash and auto-route paths;
        unused today but retained on the event payload so consumers
        that already key off them keep working.
    """
    if session_store is None:
        return

    event_type = EventType.EXPERT_RESULT if success else EventType.EXPERT_FAILURE
    event_data: dict[str, Any] = {
        "expert": expert_name,
        "success": success,
        "iterations_used": iterations_used,
    }
    if forced:
        event_data["forced"] = True
    if category:
        event_data["category"] = category
    if content is not None:
        event_data["content"] = content
    if error:
        event_data["error"] = error

    try:
        await session_store.emit_event(session_id, event_type, event_data)
    except Exception:
        logger.warning(
            "Failed to emit expert outcome event for %s",
            expert_name,
            exc_info=True,
        )
```

- [ ] **Step 6.4: Move delegation emission before endpoint validation**

Add a regression test in `tests/test_expert.py` so missing-endpoint failures
still produce a joinable `expert.delegation` row in `v_expert_outcomes`:

```python
class TestExpertServiceDelegationEvents:
    """ExpertConsultationService emits delegation before any outcome."""

    @pytest.mark.asyncio
    async def test_missing_endpoint_still_emits_delegation_then_failure(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from surogates.session.events import EventType
        from surogates.tools.builtin.expert_service import ExpertConsultationService
        from surogates.tools.loader import SkillDef

        store = AsyncMock()
        expert = SkillDef(
            name="sql_writer",
            description="Writes SQL",
            content="body",
            source="org",
            type="expert",
            expert_status="active",
            expert_endpoint=None,
        )
        service = ExpertConsultationService(
            tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={}),
            session_id=uuid4(),
            tool_registry=MagicMock(),
            session_store=store,
        )

        result = await service.consult(expert=expert, task="write a query")

        assert result.success is False
        emitted_types = [call.args[1] for call in store.emit_event.await_args_list]
        assert emitted_types == [
            EventType.EXPERT_DELEGATION,
            EventType.EXPERT_FAILURE,
        ]
```

Run just this regression and verify it fails before the implementation change:

```bash
cd /work/surogates && uv run pytest tests/test_expert.py::TestExpertServiceDelegationEvents -v
```

Expected: FAIL — current `ExpertConsultationService.consult()` validates
`expert_endpoint` before `_emit_delegation()`.

In `surogates/tools/builtin/expert_service.py:59-78`, move the `_emit_delegation`
call above the missing-endpoint guard. The beginning of `consult()` becomes:

```python
    async def consult(
        self,
        *,
        expert: SkillDef,
        task: str,
        context: str | None = None,
        forced: bool = False,
        category: str | None = None,
    ) -> ExpertConsultationResult:
        """Consult *expert* and return a structured result."""
        await self._emit_delegation(
            expert=expert,
            task=task,
            forced=forced,
            category=category,
        )

        if not expert.expert_endpoint:
            error = f"Expert '{expert.name}' has no endpoint configured."
            await self._record_failure(
                expert, error, forced=forced, category=category,
            )
            return ExpertConsultationResult(
                expert=expert.name,
                success=False,
                content=json.dumps({"error": error}),
                error=error,
            )

        try:
            result, iterations_used = await run_expert_loop(
```

- [ ] **Step 6.5: Drop the obsolete kwargs at the two call sites in `expert_service.py`**

In `surogates/tools/builtin/expert_service.py`, the calls to `record_expert_outcome` at lines 90 and 178 already do not pass `db_session` or `skill_id` (they were optional with default `None`), so the call sites compile against the new signature. Verify by inspection — if either call passes those kwargs, remove them.

```bash
cd /work/surogates && grep -n "record_expert_outcome\|db_session\|skill_id" surogates/tools/builtin/expert_service.py
```

Expected: no `db_session=` or `skill_id=` arguments at the call sites.

- [ ] **Step 6.6: Run the new tests; verify PASS**

```bash
cd /work/surogates && uv run pytest \
  tests/test_expert.py::TestRecordExpertOutcomeSlim \
  tests/test_expert.py::TestExpertServiceDelegationEvents \
  -v
```

Expected: all five tests pass.

- [ ] **Step 6.7: Run the full expert test file to surface any callers I missed**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py -q
```

Expected: pass. If any pre-existing test mocked `_update_db_stats` or imported the removed constants, delete those tests — they covered the unreachable path.

- [ ] **Step 6.8: Commit**

```bash
cd /work/surogates && git add \
  surogates/tools/builtin/expert_feedback.py \
  surogates/tools/builtin/expert_service.py \
  tests/test_expert.py
cd /work/surogates && git commit -m "refactor(experts): drop auto-disable, slim record_expert_outcome

Removes _update_db_stats, AUTO_DISABLE_THRESHOLD, MIN_USES_FOR_AUTO_DISABLE
and the db_session/skill_id parameters. The outcome recorder now only
emits EXPERT_RESULT or EXPERT_FAILURE, while ExpertConsultationService
emits EXPERT_DELEGATION before any success or failure outcome."
```

---

## Task 7: Expert branch in `expand_slash_skill`

**Why:** Lets users invoke an expert directly via `/<expert> <task>`. Service emits `expert.delegation` itself, so the caller must suppress the default `skill.invoked` emit.

**Files:**
- Modify: `surogates/harness/slash_skill.py`
- Create: `tests/test_slash_expert.py`

- [ ] **Step 7.1: Create `tests/test_slash_expert.py` with the failing test**

```python
"""Tests for the expert branch in expand_slash_skill."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from surogates.tools.loader import SkillDef


@pytest.fixture
def tenant():
    return SimpleNamespace(org_id=uuid4(), user_id=uuid4(), org_config={})


@pytest.fixture
def session_id():
    return str(uuid4())


def _expert_skill(name: str = "sql_writer") -> SkillDef:
    return SkillDef(
        name=name,
        description="Writes SQL",
        content="You are a SQL expert.",
        source="org",
        type="expert",
        expert_status="active",
        expert_endpoint="http://expert:8000/v1",
        expert_model="qwen2.5-coder-7b",
        expert_tools=["terminal"],
        trigger="SQL queries",
    )


@pytest.mark.asyncio
async def test_returns_kind_expert_and_inlines_deliverable(tenant, session_id):
    from surogates.harness.slash_skill import expand_slash_skill

    expert = _expert_skill()

    async def fake_loader(t, **kwargs):
        return [expert]

    with patch(
        "surogates.harness.slash_skill._load_skills_for_slash",
        new=fake_loader,
    ), patch(
        "surogates.tools.builtin.expert_service.ExpertConsultationService",
    ) as mock_service_cls:
        mock_service = mock_service_cls.return_value
        mock_service.consult = AsyncMock(
            return_value=SimpleNamespace(
                content="SELECT 1;",
                success=True,
                iterations_used=2,
                expert="sql_writer",
                error=None,
            ),
        )

        result = await expand_slash_skill(
            text="/sql_writer write a query for the orders table",
            tools=MagicMock(),
            tenant=tenant,
            session_id=session_id,
            api_client=None,
            session_factory=None,
            session_store=MagicMock(),
            sandbox_pool=MagicMock(),
        )

    assert result is not None
    expanded, name, staged_at, kind = result
    assert name == "sql_writer"
    assert kind == "expert"
    assert staged_at is None
    assert "[Expert sql_writer delivered:]" in expanded
    assert "SELECT 1;" in expanded
    assert "User request: write a query for the orders table" in expanded
    mock_service.consult.assert_awaited_once()


@pytest.mark.asyncio
async def test_regular_skill_path_still_returns_kind_skill(tenant, session_id):
    """Regression: regular /<skill> still goes through skill_view and returns kind='skill'."""
    import json

    from surogates.harness.slash_skill import expand_slash_skill

    regular = SkillDef(
        name="code_review",
        description="Reviews code",
        content="Review.",
        source="org",
        type="skill",
    )

    async def fake_loader(t, **kwargs):
        return [regular]

    tools = MagicMock()
    tools.dispatch = AsyncMock(
        return_value=json.dumps({
            "success": True,
            "content": "Review the code.",
            "staged_at": None,
        }),
    )

    with patch(
        "surogates.harness.slash_skill._load_skills_for_slash",
        new=fake_loader,
    ):
        result = await expand_slash_skill(
            text="/code_review src/foo.py",
            tools=tools,
            tenant=tenant,
            session_id=session_id,
            api_client=None,
            session_factory=None,
            session_store=MagicMock(),
            sandbox_pool=MagicMock(),
        )

    assert result is not None
    expanded, name, staged_at, kind = result
    assert kind == "skill"
    assert name == "code_review"
    assert "Review the code." in expanded


@pytest.mark.asyncio
async def test_inactive_expert_falls_through_to_skill_view(tenant, session_id):
    """A type=expert skill with expert_status != active uses the regular path."""
    import json

    from surogates.harness.slash_skill import expand_slash_skill

    draft = SkillDef(
        name="sql_writer",
        description="Writes SQL",
        content="body",
        source="org",
        type="expert",
        expert_status="draft",
        expert_endpoint="http://expert:8000/v1",
    )

    async def fake_loader(t, **kwargs):
        return [draft]

    tools = MagicMock()
    tools.dispatch = AsyncMock(
        return_value=json.dumps({
            "success": True,
            "content": "body",
            "staged_at": None,
        }),
    )

    with patch(
        "surogates.harness.slash_skill._load_skills_for_slash",
        new=fake_loader,
    ):
        result = await expand_slash_skill(
            text="/sql_writer hello",
            tools=tools,
            tenant=tenant,
            session_id=session_id,
            api_client=None,
            session_factory=None,
            session_store=MagicMock(),
            sandbox_pool=MagicMock(),
        )

    assert result is not None
    _, name, _, kind = result
    assert kind == "skill"
    assert name == "sql_writer"
```

- [ ] **Step 7.2: Run the new tests; verify FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_slash_expert.py -v
```

Expected: all three FAIL — `expand_slash_skill` doesn't take `session_store`/`sandbox_pool`, doesn't return a 4-tuple, and has no expert branch.

- [ ] **Step 7.3: Rewrite `surogates/harness/slash_skill.py`**

Replace the contents of `surogates/harness/slash_skill.py` with:

```python
"""Eager expansion of ``/<skill> args...`` and ``/<expert> args...`` user messages.

Two paths converge here. Regular skills inline their SKILL.md body
(staging supporting files if present). Active experts spawn a mini-loop
via :class:`ExpertConsultationService`, and the deliverable is inlined
into the user message so the base LLM reviews and relays.

The original ``/<name> args...`` message remains in the event log
untouched; only the rebuilt-in-memory message handed to the LLM is
rewritten.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final, Literal
from uuid import UUID

logger = logging.getLogger(__name__)

# ``/name`` (letters/digits/underscore/hyphen, must start with a letter),
# optionally followed by whitespace + arbitrary text.  ``re.DOTALL`` lets
# the trailing args span newlines (multi-line user messages).
_SLASH_COMMAND_RE: Final = re.compile(
    r"^/([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+(.*))?$",
    re.DOTALL,
)

# Slash commands handled elsewhere in the loop -- never treat these as skills.
_BUILTIN_SLASH_COMMANDS: Final[frozenset[str]] = frozenset({
    "clear",
    "compress",
    "goal",
    "loop",
    "mission",
})


def parse_slash_command(text: str) -> tuple[str, str] | None:
    """Return ``(name, args)`` if *text* is a slash command, else ``None``.

    Builtin commands (``/clear``, ``/compress``) return ``None`` so they
    flow through to their dedicated handlers in the harness loop.
    """
    match = _SLASH_COMMAND_RE.match(text.strip())
    if match is None:
        return None
    name = match.group(1)
    if name in _BUILTIN_SLASH_COMMANDS:
        return None
    args = (match.group(2) or "").strip()
    return name, args


def build_expanded_message(*, name: str, args: str, skill_body: str) -> str:
    """Build the rewritten user message with the skill body inlined.

    The ``skill_body`` should be the ``content`` field returned by
    ``skill_view``; in production it already starts with a staged-path
    preamble so relative paths (``scripts/foo.py``) resolve correctly.
    """
    lines: list[str] = [
        f"The user invoked the `{name}` skill"
        + (f" with: {args}" if args else "")
        + ".",
        "",
        "Use the following skill to handle this request:",
        "",
        "---",
        skill_body,
        "---",
    ]
    if args:
        lines.extend(["", f"User request: {args}"])
    return "\n".join(lines)


def build_expert_expanded_message(
    *, name: str, args: str, deliverable: str,
) -> str:
    """Build the rewritten user message with the expert deliverable inlined.

    The deliverable is presented as the expert's reply; the base LLM
    reviews and relays in the same turn.
    """
    return (
        f"[Expert {name} delivered:]\n"
        f"{deliverable}\n\n"
        f"User request: {args}"
    )


async def _load_skills_for_slash(tenant: Any, **kwargs: Any) -> list:
    """Load the tenant skill catalog for slash-command resolution.

    Wraps ``surogates.tools.builtin.skills._load_all_skills`` so tests
    can monkey-patch this single seam without going through the
    underlying dispatch.
    """
    from surogates.tools.builtin.skills import _load_all_skills

    return await _load_all_skills(tenant, **kwargs)


async def expand_slash_skill(
    *,
    text: str,
    tools: Any,
    tenant: Any,
    session_id: str,
    api_client: Any | None,
    session_factory: Any | None,
    session_store: Any | None = None,
    sandbox_pool: Any | None = None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
    """Try to expand a ``/<name> args...`` user message.

    Returns ``(expanded_text, name, staged_at, kind)`` on success, or
    ``None`` when *text* is not a slash command, names a builtin, names
    an unknown skill/expert, or expansion failed.  ``kind`` is
    ``"expert"`` when an active expert handled the invocation,
    otherwise ``"skill"``.

    The function never raises -- failures degrade to ``None`` so the
    original user message reaches the LLM unchanged.
    """
    parsed = parse_slash_command(text)
    if parsed is None:
        return None
    name, args = parsed

    # Look up the named entry in the tenant catalog so we can branch
    # on type before dispatching skill_view.
    try:
        catalog = await _load_skills_for_slash(
            tenant,
            api_client=api_client,
            session_factory=session_factory,
        )
    except Exception:
        logger.debug(
            "Slash catalog load failed for /%s; falling back to skill path",
            name,
            exc_info=True,
        )
        catalog = []

    matched = next((s for s in catalog if s.name == name), None)
    if matched is not None and getattr(matched, "is_active_expert", False):
        return await _expand_expert(
            expert=matched,
            args=args,
            tenant=tenant,
            session_id=session_id,
            tool_registry=tools,
            session_store=session_store,
            sandbox_pool=sandbox_pool,
        )

    return await _expand_skill(
        name=name,
        args=args,
        tools=tools,
        tenant=tenant,
        session_id=session_id,
        api_client=api_client,
        session_factory=session_factory,
    )


async def _expand_skill(
    *,
    name: str,
    args: str,
    tools: Any,
    tenant: Any,
    session_id: str,
    api_client: Any | None,
    session_factory: Any | None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
    """Inline a regular skill's body via ``skill_view``.

    Returns ``None`` when the skill is unknown or staging failed so the
    caller falls through to the verbatim user message.
    """
    try:
        result = await tools.dispatch(
            "skill_view",
            {"name": name},
            tenant=tenant,
            session_id=session_id,
            api_client=api_client,
            session_factory=session_factory,
        )
    except Exception:
        logger.debug(
            "skill_view dispatch failed for /%s; passing through verbatim",
            name,
            exc_info=True,
        )
        return None

    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        logger.debug("skill_view returned non-JSON for /%s", name)
        return None

    if not payload.get("success"):
        return None

    skill_body = payload.get("content") or ""
    if not skill_body:
        return None
    staged_at = payload.get("staged_at")

    expanded = build_expanded_message(name=name, args=args, skill_body=skill_body)
    return expanded, name, staged_at, "skill"


async def _expand_expert(
    *,
    expert: Any,
    args: str,
    tenant: Any,
    session_id: str,
    tool_registry: Any,
    session_store: Any | None,
    sandbox_pool: Any | None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
    """Run the expert mini-loop and inline the deliverable.

    Falls back to ``None`` on errors so the user sees the original
    ``/<expert>`` text and can retry.
    """
    if not args:
        # Nothing to consult about -- behave like an unknown skill so the
        # LLM (or user) gets a chance to clarify.
        return None

    try:
        from surogates.tools.builtin.expert_service import ExpertConsultationService

        service = ExpertConsultationService(
            tenant=tenant,
            session_id=UUID(session_id),
            tool_registry=tool_registry,
            session_store=session_store,
            sandbox_pool=sandbox_pool,
        )
        outcome = await service.consult(expert=expert, task=args)
    except Exception:
        logger.exception(
            "Expert consultation failed for /%s; passing through verbatim",
            expert.name,
        )
        return None

    deliverable = outcome.content if outcome.success else (outcome.content or "")
    expanded = build_expert_expanded_message(
        name=expert.name, args=args, deliverable=deliverable,
    )
    return expanded, expert.name, None, "expert"
```

Key shape changes the caller must absorb:
- Return type is now `tuple[str, str, str | None, Literal["skill", "expert"]] | None`.
- New keyword arguments `session_store` and `sandbox_pool` (both optional, defaulted to `None` for backward compat in tests).

- [ ] **Step 7.4: Run the new tests; verify PASS**

```bash
cd /work/surogates && uv run pytest tests/test_slash_expert.py -v
```

Expected: all three pass.

- [ ] **Step 7.5: Commit**

```bash
cd /work/surogates && git add \
  surogates/harness/slash_skill.py \
  tests/test_slash_expert.py
cd /work/surogates && git commit -m "feat(experts): slash command runs expert mini-loop

expand_slash_skill now detects type=expert active skills, runs the
ExpertConsultationService, and inlines the deliverable as a synthetic
user message. Returns a kind discriminator so the caller can avoid
double-emitting skill.invoked."
```

---

## Task 8: Wire the new `expand_slash_skill` shape into the harness loop

**Why:** Pass through `session_store` and `sandbox_pool`; consume the new 4-tuple; suppress `skill.invoked` emission for expert kinds (the service emits `expert.delegation`).

**Files:**
- Modify: `surogates/harness/loop.py:1299-1338`

- [ ] **Step 8.1: Write a regression test for the conditional event emission**

Append to `tests/test_slash_expert.py`:

```python
@pytest.mark.asyncio
async def test_loop_does_not_emit_skill_invoked_for_expert_kind():
    """The harness's slash dispatch must not emit SKILL_INVOKED for kind='expert'."""
    from unittest.mock import AsyncMock

    # We exercise the conditional logic by importing the function the loop
    # uses to gate the emit. Since the gate lives inline in loop.py, we
    # assert the post-condition by inspecting the source for the kind check.
    import inspect

    from surogates.harness import loop as loop_mod

    src = inspect.getsource(loop_mod)
    # The conditional must reference the `kind` field returned by
    # expand_slash_skill and gate the SKILL_INVOKED emission on it.
    assert 'kind == "skill"' in src or "kind == 'skill'" in src
    # And it must pass session_store + sandbox_pool to expand_slash_skill.
    assert "session_store=" in src
    assert "sandbox_pool=" in src
```

This is a structural assertion (not behavioral) because the loop's
dispatch is entangled with the full session lifecycle; a behavioral
test for this single branch is covered by the existing per-event
integration tests.

- [ ] **Step 8.2: Run the test; verify FAIL**

```bash
cd /work/surogates && uv run pytest tests/test_slash_expert.py::test_loop_does_not_emit_skill_invoked_for_expert_kind -v
```

Expected: FAIL — `loop.py` doesn't pass those kwargs and doesn't gate the emit on `kind`.

- [ ] **Step 8.3: Update the call site in `surogates/harness/loop.py:1299-1338`**

Replace the block:

```python
            # 10b. Eager /<skill> expansion -- see slash_skill.expand_slash_skill.
            if last_user_content.startswith("/"):
                expansion = await expand_slash_skill(
                    text=last_user_content,
                    tools=self._tools,
                    tenant=self._tenant,
                    session_id=str(session.id),
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                )
                if expansion is not None:
                    expanded_text, skill_name, staged_at = expansion
                    last_user["content"] = expanded_text
                    # Suppress duplicate audit events on crash-recovery wakes.
                    # skill_view itself is idempotent (staging short-circuits via
                    # an exists() check), but the SKILL_INVOKED event log row is
                    # not -- so guard it by scanning prior events.
                    already_emitted = any(
                        e.type == EventType.SKILL_INVOKED.value
                        and e.data.get("raw_message") == last_user_content
                        for e in all_events
                    )
                    if not already_emitted:
                        try:
                            await self._store.emit_event(
                                session.id,
                                EventType.SKILL_INVOKED,
                                {
                                    "skill": skill_name,
                                    "raw_message": last_user_content,
                                    "staged_at": staged_at,
                                },
                            )
                        except Exception:
                            logger.exception(
                                "Failed to emit SKILL_INVOKED audit event "
                                "for session %s skill=%s",
                                session.id, skill_name,
                            )
```

with:

```python
            # 10b. Eager /<skill> or /<expert> expansion.
            # See slash_skill.expand_slash_skill. ``kind`` distinguishes
            # the two paths so we don't double-emit a skill.invoked when
            # the service already emitted expert.delegation.
            if last_user_content.startswith("/"):
                expansion = await expand_slash_skill(
                    text=last_user_content,
                    tools=self._tools,
                    tenant=self._tenant,
                    session_id=str(session.id),
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                    session_store=self._store,
                    sandbox_pool=self._sandbox_pool,
                )
                if expansion is not None:
                    expanded_text, skill_name, staged_at, kind = expansion
                    last_user["content"] = expanded_text
                    if kind == "skill":
                        # Suppress duplicate audit events on crash-recovery wakes.
                        # skill_view itself is idempotent (staging short-circuits via
                        # an exists() check), but the SKILL_INVOKED event log row is
                        # not -- so guard it by scanning prior events.
                        already_emitted = any(
                            e.type == EventType.SKILL_INVOKED.value
                            and e.data.get("raw_message") == last_user_content
                            for e in all_events
                        )
                        if not already_emitted:
                            try:
                                await self._store.emit_event(
                                    session.id,
                                    EventType.SKILL_INVOKED,
                                    {
                                        "skill": skill_name,
                                        "raw_message": last_user_content,
                                        "staged_at": staged_at,
                                    },
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to emit SKILL_INVOKED audit event "
                                    "for session %s skill=%s",
                                    session.id, skill_name,
                                )
                    # kind == "expert": the ExpertConsultationService has
                    # already emitted expert.delegation and (later) expert.result
                    # or expert.failure, so we intentionally skip the
                    # SKILL_INVOKED row here.
```

- [ ] **Step 8.4: Run the structural test; verify PASS**

```bash
cd /work/surogates && uv run pytest tests/test_slash_expert.py::test_loop_does_not_emit_skill_invoked_for_expert_kind -v
```

Expected: pass.

- [ ] **Step 8.5: Run the loop / harness test files to catch tuple-unpacking regressions**

```bash
cd /work/surogates && uv run pytest tests/test_slash_expert.py tests/test_expert.py -q
```

Expected: pass. If the codebase has other call sites of `expand_slash_skill` outside `loop.py`, find and update them:

```bash
cd /work/surogates && grep -rn "expand_slash_skill" surogates/ tests/ --include="*.py"
```

The expected call sites are `surogates/harness/loop.py` (production) and the new `tests/test_slash_expert.py`. Update any other caller to the 4-tuple shape; if none exist, the search is informational.

- [ ] **Step 8.6: Commit**

```bash
cd /work/surogates && git add \
  surogates/harness/loop.py \
  tests/test_slash_expert.py
cd /work/surogates && git commit -m "feat(experts): wire slash expert path into harness loop

Pass session_store + sandbox_pool through expand_slash_skill, and
gate SKILL_INVOKED emission on kind so the expert branch (which
emits expert.delegation via the service) does not double-log."
```

---

## Task 9: Excise dead auto-router and forced-route shims

**Why:** With auto-routing dropped (spec § Non-Goals), the helpers are confirmed dead. Excise to reduce cognitive load.

**Files:**
- Modify: `surogates/harness/expert_routing.py`
- Modify: `surogates/harness/loop.py:3649-3679`
- Modify: `tests/test_expert_routing.py`

- [ ] **Step 9.1: Identify what stays in `expert_routing.py`**

The following symbols are imported by `harness/loop.py` and `harness/llm_call.py` and `harness/self_discover.py` — they must remain:

- `classify_hard_task_async`, `_build_classifier_payload`, `HardTaskClassification`, `HARD_TASK_CATEGORIES`, `HardTaskJudgment` — used by the thinking gate, SELF-DISCOVER scaffold, and advisor pre-pass.
- `model_supports_thinking_toggle`, `build_thinking_extra_body`, `merge_extra_body` — used by `llm_call.py`.
- `classify_hard_task` (the regex fallback) — invoked from `classify_hard_task_async` when the auxiliary LLM is unavailable.

The following are dead:

- `select_expert_for_task`
- `load_skills_for_expert_routing`
- `classify_tool_calls`
- `_TRIGGER_SPLIT_RE`, `_WORD_RE`, `_TRIGGER_STOPWORDS`, `_normalise_trigger_text`, `_trigger_match_score`

Confirm by searching:

```bash
cd /work/surogates && grep -rn "select_expert_for_task\|load_skills_for_expert_routing\|classify_tool_calls\|_trigger_match_score" surogates/ --include="*.py"
```

Expected: only references inside `surogates/harness/expert_routing.py` itself.

- [ ] **Step 9.2: Add a removal-regression test**

Append to `tests/test_expert_routing.py` (at the top of the file, after the imports):

```python
class TestDeadHelpersRemoved:
    """Auto-router helpers must be gone — the design dropped auto-routing."""

    def test_select_expert_for_task_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "select_expert_for_task")

    def test_load_skills_for_expert_routing_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "load_skills_for_expert_routing")

    def test_classify_tool_calls_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "classify_tool_calls")

    def test_trigger_helpers_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "_normalise_trigger_text")
        assert not hasattr(expert_routing, "_trigger_match_score")
```

- [ ] **Step 9.3: Delete the existing tests covering the removed helpers**

Open `tests/test_expert_routing.py` and find the test classes / functions that import:

- `classify_tool_calls` (around lines 111 and 121)
- `select_expert_for_task` (around lines 133, 147, 158, 168)

Delete those tests (the surrounding `class TestClassifyToolCalls` / `class TestSelectExpertForTask` if they exist as classes; otherwise the individual `def test_*` functions). Keep all tests for `classify_hard_task` and the thinking-toggle helpers.

- [ ] **Step 9.4: Run the trimmed test file; verify FAIL for the new regressions**

```bash
cd /work/surogates && uv run pytest tests/test_expert_routing.py -v
```

Expected: `TestDeadHelpersRemoved` tests FAIL (symbols still present); remaining tests pass.

- [ ] **Step 9.5: Delete the dead symbols in `surogates/harness/expert_routing.py`**

Remove the following from the file:

- Lines around 120-122: `_TRIGGER_SPLIT_RE`, `_WORD_RE`, `_TRIGGER_STOPWORDS` module-level constants.
- The function `classify_tool_calls` (lines 107-117).
- The function `select_expert_for_task` (lines 128-148).
- The helpers `_normalise_trigger_text` (lines 151-152) and `_trigger_match_score` (lines 155-179).
- The function `load_skills_for_expert_routing` (lines 182-194).

Keep everything else in the file. After the edits, the imports section at the top no longer needs `OrderedDict` or `Iterable` if they were only used by the removed code — keep them if still used by `_ClassifierCache` or `classify_hard_task_async`. Run a quick grep:

```bash
cd /work/surogates && grep -n "OrderedDict\|Iterable" surogates/harness/expert_routing.py
```

Drop any import that has zero remaining references.

- [ ] **Step 9.6: Delete the forced-route shims in `surogates/harness/loop.py`**

In `_maybe_consult_required_advisor` around `surogates/harness/loop.py:3453`,
replace the forced-expert shim call:

```python
            or classification.category in self._forced_expert_categories_after_latest_user(
                all_events,
            )
```

with the real advisor helper:

```python
            or classification.category in self._advisor_categories_after_latest_user(
                all_events,
            )
```

Then remove the three static methods at `surogates/harness/loop.py:3649-3679`:

- `_has_forced_expert_after_latest_user`
- `_forced_expert_categories_after_latest_user`
- `_legacy_forced_expert_categories_after_latest_user`

If anything else in `loop.py` calls these methods (search before deleting):

```bash
cd /work/surogates && grep -n "_forced_expert_categories_after_latest_user\|_has_forced_expert_after_latest_user\|_legacy_forced_expert_categories_after_latest_user" surogates/
```

Expected: no references remain after the replacement and deletion.

- [ ] **Step 9.7: Run the trimmed test file; verify PASS**

```bash
cd /work/surogates && uv run pytest tests/test_expert_routing.py -v
```

Expected: all pass, including `TestDeadHelpersRemoved`.

- [ ] **Step 9.8: Run the full harness test surface to catch import-side breakage**

```bash
cd /work/surogates && uv run pytest tests/test_expert.py tests/test_expert_routing.py tests/test_slash_expert.py -q
```

Expected: pass.

- [ ] **Step 9.9: Commit**

```bash
cd /work/surogates && git add \
  surogates/harness/expert_routing.py \
  surogates/harness/loop.py \
  tests/test_expert_routing.py
cd /work/surogates && git commit -m "refactor(experts): excise dead auto-router and forced-route shims

select_expert_for_task, load_skills_for_expert_routing,
classify_tool_calls, the trigger-scoring helpers, and the three
forced-route compat methods on AgentHarness all served the auto-router
that the design explicitly drops."
```

---

## Task 10: Documentation updates

**Why:** Spec § "Documentation updates" — four edits to `docs/experts/index.md` so the published docs match the resurrected runtime.

**Files:**
- Modify: `docs/experts/index.md`

- [ ] **Step 10.1: "What is an Expert?" paragraph**

In `docs/experts/index.md` around lines 5-7, locate:

> "The harness automatically consults a matching active expert for hard tasks before the default LLM answers or uses tools. The default LLM can also explicitly delegate to an expert via the `consult_expert` tool and receives the expert's result back for review."

Replace the entire paragraph (both sentences) with:

> "The base LLM consults an expert via the `consult_expert` tool when a task falls within its specialty. Users can also invoke an expert directly with `/<expert-name> <task>`; in both paths the deliverable flows back through the base LLM for review and relay."

- [ ] **Step 10.2: Design Principle #1**

Find the line beginning `1. **Hard tasks are expert-routed.**` (around line 37) and replace the entire principle (the sentence after the bold label) with:

```markdown
1. **Experts are consulted voluntarily.** The base LLM uses `consult_expert` when a task falls within an active expert's specialty; users can invoke experts directly via `/<expert>` slash command. The harness does not auto-route — the advisor handles strategic guidance for hard tasks.
```

- [ ] **Step 10.3: Delete the "Auto-Disable" subsection**

Around line 362-368, find:

```markdown
### Auto-Disable

Once an expert accumulates at least 20 invocations, the platform monitors its success rate. If the rate drops below the configured threshold (default: 60%), the expert is automatically disabled and its status is set to `retired`. The admin can retrain or reconfigure the expert externally and reactivate it.

**Success** means the session completed normally after expert delegation and the user did not override or redo the expert's work.

**Failure** means the expert hit its iteration limit, raised an error, or the user explicitly corrected the expert's output.
```

Replace with:

```markdown
### Telemetry and quality signals

Every consultation emits `expert.delegation` followed by `expert.result` (success) or `expert.failure`. When a user or judge submits feedback on an `expert.result` event via the feedback API, `expert.endorse` / `expert.override` is appended. Together these populate the `v_expert_outcomes` SQL view and feed the training collector and downstream quality dashboards.

The platform does not auto-disable experts. Operators retire an expert manually via `POST /v1/skills/{name}/retire` when its quality signals warrant it.
```

- [ ] **Step 10.4: Add a "Slash invocation" subsection**

Locate the heading `## 5. Verify It Works` (around line 314) and find its closing — the section ends with the `curl "http://localhost:8000/v1/sessions/$SESSION_ID/events?type=expert.delegation" ...` example (around line 336). After that fenced code block and before `## 6. Monitor and Maintain`, insert:

```markdown
### Slash invocation

Users can invoke an expert directly; the base LLM still reviews and relays
the deliverable:

```
User: /sql_writer write me a query for the orders table

Expert sql_writer (mini-loop):
  -> terminal: psql -c "\d orders"
  -> returns: "SELECT ... FROM orders ..."

Base LLM (reviews and relays):
  Here's the query the sql_writer expert produced: ...
```

The mini-loop's deliverable is injected as a synthetic user message that the base LLM sees in the same turn, so it can review, adjust, or relay the expert's output. Slash invocation emits the same `expert.delegation` / `expert.result` event sequence as the `consult_expert` tool path; the only difference is who initiates the call.
```

- [ ] **Step 10.5: Verify the doc renders cleanly**

```bash
cd /work/surogates && grep -n "Auto-Disable\|harness automatically consults\|Hard tasks are expert-routed" docs/experts/index.md
```

Expected: empty output (none of the old phrases remain).

```bash
cd /work/surogates && grep -n "consult an expert\|Slash invocation\|expert.delegation" docs/experts/index.md | head -10
```

Expected: at least one match per phrase, confirming the new content landed.

- [ ] **Step 10.6: Commit**

```bash
cd /work/surogates && git add docs/experts/index.md
cd /work/surogates && git commit -m "docs(experts): align with the voluntary-consultation runtime

- Drop the auto-routing claim from the intro.
- Reword Design Principle #1 (voluntary consultation, not auto-routed).
- Replace the Auto-Disable subsection with a telemetry note pointing
  at v_expert_outcomes and the manual retire endpoint.
- Add a Slash invocation subsection under Verify It Works."
```

---

## Task 11: Final verification

**Why:** All the per-task tests pass; this catches any cross-cutting regressions before opening a PR.

- [ ] **Step 11.1: Run the entire test suite**

```bash
cd /work/surogates && uv run pytest -q 2>&1 | tail -40
```

Expected: 0 failures. If anything red appears, inspect — likely a tuple-unpacking site or an import that referenced a removed symbol.

- [ ] **Step 11.2: Verify the prompt fragment renders end-to-end**

```bash
cd /work/surogates && uv run python -c "
from types import SimpleNamespace
from uuid import uuid4
from surogates.harness.prompt import PromptBuilder
from surogates.tools.loader import SkillDef

tenant = SimpleNamespace(
    org_id=uuid4(), user_id=uuid4(),
    org_config={'default_model': 'gpt-4o'},
    user_preferences={},
    asset_root='/tmp/x',
)
skills = [SkillDef(
    name='sql_writer', description='Writes SQL queries',
    content='body', source='org', type='expert',
    expert_status='active', trigger='SQL queries, database schemas',
)]
pb = PromptBuilder(tenant=tenant, skills=skills)
print(pb._available_experts_section())
"
```

Expected output: the rendered `# Available Experts` block with `sql_writer` listed and the "Do NOT use delegate_task" disambiguation line.

- [ ] **Step 11.3: Verify the tool is reachable from `ToolRuntime`**

```bash
cd /work/surogates && uv run python -c "
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
reg = ToolRegistry()
runtime = ToolRuntime(reg)
runtime.register_builtins()
assert reg.has('consult_expert'), 'consult_expert not registered'
print('OK: consult_expert registered with schema:', reg.get('consult_expert').schema.name)
"
```

Expected: `OK: consult_expert registered with schema: consult_expert`.

- [ ] **Step 11.4: Push and open a PR (manual step — do not auto-push)**

```bash
cd /work/surogates && git log --oneline origin/master..HEAD
```

Expected: the per-task commits land in order (Task 2 through Task 10). Open a PR manually via the GitHub UI or `gh pr create` when you're ready.

---

## Cross-task notes

- **Spec coverage:**
  - Goal #1 (LLM tool call) → Task 2, Task 3
  - Goal #2 (slash command) → Task 7, Task 8
  - Goal #3 (discoverability) → Task 4 (`skills_list`), Task 5 (prompt section), Task 2 (registration)
  - Goal #4 (event emission) → Task 6 (slim outcome recorder + delegation-before-outcome ordering in `ExpertConsultationService.consult()`).
  - Goal #5 (excise dead code) → Task 9
  - Non-goal "no auto-disable" → Task 6
  - Non-goal "no auto-routing" → preserved by *not* re-wiring `select_expert_for_task`; Task 9 makes this explicit.
  - Vocabulary fix → Task 3, Task 5
  - Documentation updates → Task 10

- **No placeholders:** every code step shows exact code; every command shows expected output; every commit message is fully spelled.

- **Type consistency:** the `kind` discriminator is `Literal["skill", "expert"]` in `slash_skill.py` (Task 7) and consumed verbatim in `loop.py` (Task 8). `record_expert_outcome`'s parameter set (Task 6) matches the call sites in `expert_service.py:90, 178`.

- **YAGNI:** no new tenant config knobs (auto-disable thresholds), no vault wiring, no sub-sandbox, no auto-routing — all explicitly out of scope per the spec.

- **TDD:** every task starts with a failing test before the implementation step.
