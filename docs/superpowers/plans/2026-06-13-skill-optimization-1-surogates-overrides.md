# Skill Optimization Plan 1: Surogates Session-Scoped Skill Overrides Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Execution Todo List

- [x] Task 1: `SkillOverride` schema + `skill_overrides` field on `PromptRequest`
- [x] Task 2: Feature flag `skill_overrides_enabled`
- [x] Task 3: Store `skill_overrides` into `session.config` at prompt submission (also repaired the stale `test_prompts_api.py` `app` fixture, which never wired agent resolution — the route now requires an agent; 3 unrelated `/v1/memory` storage path-traversal failures remain pre-existing)
- [x] Task 4: Loader override layer (`_apply_overrides` + `load_skills(overrides=)`) — note `tests/test_loader.py` (stale `MCPServerDef` import) and 3 `test_resource_loader_bundle.py` cases fail pre-existing, unaffected by the override param (verified by stash)
- [x] Task 5: API `view_skill` resolves overrides (shared-runtime path) — integration tests use a fake Hub bundle + `RuntimeConfigCache` (requests carry `agent_id`, as the real Work UI/SkillOpt worker do) instead of the dead `platform_skills_dir`
- [x] Task 6: API `list_skills` honors overrides (catalog completeness)
- [x] Task 7: `HarnessAPIClient.list_skills` forwards `session_id`
- [x] Task 8: Slash `/<skill>` expansion uses override content (coverage)
- [x] Task 9: Audit — enrich `skill.invoked` when an override was used
- [ ] Task 10: Worker-local `skill_view` applies overrides (dedicated-agent path)
- [ ] Task 11: Full-suite verification

> **Execution note (discovered during implementation):** the integration-test reference `tests/integration/test_skills_sa_token.py` is stale on this branch — it sets `settings.platform_skills_dir`, a field that no longer exists on `Settings` (platform skills are now bundle-only), so it errors at fixture setup. Tasks 5/6 therefore wire a fake Hub bundle + `RuntimeConfigCache` instead of the dead `platform_skills_dir` path.

**Goal:** Let an authorized service-account prompt submission attach a candidate `SKILL.md` body to a single Surogates session, so that session's `/<skill>` expansion and `skill_view` resolve the candidate content instead of the published skill — without mutating any skill catalog or redeploying the agent.

**Architecture:** A new `skill_overrides` map is stored on `session.config` at prompt-submission time (gated to service accounts + a feature flag). Skill resolution gains a single highest-precedence override layer applied via `dataclasses.replace`, which patches the candidate's `content`/`description`/`trigger` onto the *original* `SkillDef` while preserving its `source` — so supporting-file staging continues to work from the original bundle/storage. The shared-runtime path (API skill routes) and the worker-local path (`_skill_view_handler`) both read the session's overrides and pass them into `ResourceLoader.load_skills(overrides=...)`. The harness audit trail records that an override was used.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, async SQLAlchemy (JSONB `session.config`), pytest/`httpx.AsyncClient`. This is the first of three plans; it is the dependency for the ops-side SkillOpt backend (Plan 2).

**Repo:** `/work/surogates/` — run tests with `uv run pytest`.

---

## Spec Reference

Design: `/work/surogate-ops/docs/superpowers/specs/2026-05-27-skill-optimization-design.md`, sections "Surogates Harness/API Design", "Skill Resolution Precedence", "Affected Surogates Components", "Auditability", "Security And Isolation", "Testing Strategy → Surogates".

## Key existing code (read before starting)

| What | Where |
|------|-------|
| `PromptRequest` model | `surogates/api/routes/prompts.py:57` |
| `_require_service_account()` | `surogates/api/routes/prompts.py:109` |
| `_submit_one()` config build | `surogates/api/routes/prompts.py:176` |
| `ResourceLoader.load_skills()` | `surogates/tools/loader.py:195` |
| `ResourceLoader._merge()` | `surogates/tools/loader.py:723` |
| `SkillDef` dataclass | `surogates/tools/loader.py:92` |
| skill source constants | `surogates/tools/loader.py:49` |
| API `list_skills` route | `surogates/api/routes/skills.py:382` |
| API `view_skill` route | `surogates/api/routes/skills.py:432` |
| `_stage_skill_for_session()` | `surogates/api/routes/skills.py:253` |
| `HarnessAPIClient` | `surogates/harness/api_client.py:20` |
| `expand_slash_skill()` / `_BUILTIN_SLASH_COMMANDS` | `surogates/harness/slash_skill.py:178` / `:32` |
| `_skill_view_handler()` | `surogates/tools/builtin/skills.py:292` |
| `skill.invoked` emit | `surogates/harness/loop.py:968` |
| `Session.config` column | `surogates/db/models.py:226` |

## File Structure

- **Modify** `surogates/api/routes/prompts.py` — add `SkillOverride` model, `skill_overrides` field, feature-flag gate, store into `session.config`.
- **Modify** `surogates/tools/loader.py` — add `_apply_overrides()` + `overrides=` param on `load_skills()`.
- **Modify** `surogates/api/routes/skills.py` — read session overrides in `list_skills`/`view_skill`, pass to loader.
- **Modify** `surogates/harness/api_client.py` — forward `session_id` on `list_skills`.
- **Modify** `surogates/tools/builtin/skills.py` — apply overrides in the worker-local `_skill_view_handler`/`_load_all_skills`.
- **Modify** `surogates/harness/loop.py` — enrich the `skill.invoked` event when an override was used.
- **Modify** `surogates/config.py` — add `WorkerSettings.skill_overrides_enabled` kill switch (`SUROGATES_WORKER_SKILL_OVERRIDES_ENABLED`).
- **Modify** `surogates/harness/slash_skill.py` — pass local-mode `session_config` into slash catalog loading and `skill_view` dispatch so overrides are available when no API client is present.
- **Tests** under the repo's existing layout (`tests/test_prompts_schema.py`, `tests/integration/test_prompts_api.py`, `tests/test_loader_overrides.py`, `tests/integration/test_skills_overrides.py`, `tests/test_harness_api_client.py`, `tests/test_slash_skill.py`, `tests/test_loop_skill_invoked_override.py`, `tests/test_builtin_skills_overrides.py`).

---

## Task 1: `SkillOverride` schema + `skill_overrides` field on `PromptRequest`

**Files:**
- Modify: `surogates/api/routes/prompts.py:57`
- Test: `tests/test_prompts_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prompts_schema.py
from surogates.api.routes.prompts import PromptRequest, SkillOverride


def test_skill_override_defaults():
    ov = SkillOverride(content="# candidate body")
    assert ov.content == "# candidate body"
    assert ov.type == "skill"
    assert ov.source == "skillopt"
    assert ov.description is None
    assert ov.run_id is None


def test_prompt_request_accepts_skill_overrides():
    req = PromptRequest(
        prompt="/browser-research compare vendors",
        skill_overrides={
            "browser-research": {
                "content": "# improved body",
                "description": "Research and summarize web information.",
                "trigger": "research, web lookup",
                "run_id": "run-1",
                "candidate_id": "cand-2",
            }
        },
    )
    assert req.skill_overrides is not None
    ov = req.skill_overrides["browser-research"]
    assert ov.content == "# improved body"
    assert ov.run_id == "run-1"


def test_prompt_request_skill_overrides_optional():
    req = PromptRequest(prompt="hello")
    assert req.skill_overrides is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prompts_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'SkillOverride'`.

- [ ] **Step 3: Add the model and field**

In `surogates/api/routes/prompts.py`, add near the top with the other imports:

```python
from pydantic import BaseModel, Field
```

(`BaseModel`/`Field` are already imported — confirm, don't duplicate.) Add the `SkillOverride` model immediately above `class PromptRequest`:

```python
class SkillOverride(BaseModel):
    """A session-scoped candidate replacement for one skill's body.

    Only service-account prompt submissions may attach these (see
    ``_require_service_account``).  The override replaces the skill's
    ``SKILL.md`` body for THIS session only; supporting files
    (``scripts/``, ``references/`` …) continue to stage from the
    published skill source.  Used by the ops Skill-Optimization worker
    to evaluate a candidate without mutating the catalog or redeploying
    the agent.
    """

    content: str = Field(..., min_length=1)
    description: str | None = None
    trigger: str | None = None
    type: str = "skill"
    source: str = "skillopt"
    run_id: str | None = None
    candidate_id: str | None = None
```

Then add the field to `PromptRequest` (after `metadata`):

```python
    skill_overrides: dict[str, SkillOverride] | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prompts_schema.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/prompts.py tests/test_prompts_schema.py
git commit -m "feat(prompts): add SkillOverride schema and skill_overrides field"
```

---

## Task 2: Feature flag `skill_overrides_enabled`

**Files:**
- Modify: `surogates/config.py:232` (`WorkerSettings`)
- Test: `tests/test_worker_settings_skill_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker_settings_skill_overrides.py
from surogates.config import WorkerSettings


def test_skill_overrides_enabled_defaults_true():
    s = WorkerSettings()
    assert s.skill_overrides_enabled is True


def test_skill_overrides_enabled_env_override(monkeypatch):
    monkeypatch.setenv("SUROGATES_WORKER_SKILL_OVERRIDES_ENABLED", "false")
    s = WorkerSettings()
    assert s.skill_overrides_enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_settings_skill_overrides.py -v`
Expected: FAIL — `AttributeError: 'WorkerSettings' object has no attribute 'skill_overrides_enabled'`.

- [ ] **Step 3: Add the flag**

In `surogates/config.py`, add this field to `WorkerSettings` near the other worker kill switches (`emit_turn_summaries`):

```python
    # When False, service-account ``skill_overrides`` on prompt submissions
    # are ignored by the API and worker-local skill resolution.
    skill_overrides_enabled: bool = True
```

Because `WorkerSettings.model_config = {"env_prefix": "SUROGATES_WORKER_"}`, the env var is `SUROGATES_WORKER_SKILL_OVERRIDES_ENABLED`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_settings_skill_overrides.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/config.py tests/test_worker_settings_skill_overrides.py
git commit -m "feat(config): add skill_overrides_enabled feature flag"
```

---

## Task 3: Store `skill_overrides` into `session.config` at prompt submission

**Files:**
- Modify: `surogates/api/routes/prompts.py:176` (the `config` build in `_submit_one`)
- Test: `tests/integration/test_prompts_api.py`

- [ ] **Step 1: Write the failing test**

Mirror the existing `test_submit_prompt_creates_api_channel_session` fixtures (`client`, `session_factory`, `session_store`, `create_org`, `_issue_token`) in `tests/integration/test_prompts_api.py`:

```python
async def test_skill_overrides_stored_for_service_account(
    client, session_factory, session_store
):
    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)  # service-account token

    resp = await client.post(
        "/v1/api/prompts",
        json={
            "prompt": "/browser-research compare vendors",
            "metadata": {"skillopt_run_id": "run-1"},
            "skill_overrides": {
                "browser-research": {
                    "content": "# candidate body",
                    "run_id": "run-1",
                    "candidate_id": "cand-2",
                }
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202, resp.text

    sid = UUID(resp.json()["session_id"])
    session = await session_store.get_session(sid)
    assert session.config["skill_overrides"]["browser-research"]["content"] == "# candidate body"
    assert session.config["skill_overrides"]["browser-research"]["candidate_id"] == "cand-2"
    # pipeline_metadata still lands alongside it.
    assert session.config["pipeline_metadata"] == {"skillopt_run_id": "run-1"}


async def test_skill_overrides_dropped_when_flag_disabled(
    client, session_factory, session_store, monkeypatch
):
    monkeypatch.setenv("SUROGATES_WORKER_SKILL_OVERRIDES_ENABLED", "false")
    org_id = await create_org(session_factory)
    token = await _issue_token(session_factory, org_id)

    resp = await client.post(
        "/v1/api/prompts",
        json={
            "prompt": "hello",
            "skill_overrides": {"x": {"content": "y"}},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202
    sid = UUID(resp.json()["session_id"])
    session = await session_store.get_session(sid)
    assert "skill_overrides" not in session.config
```

> Note: if `Settings` is constructed once at app startup, the flag-disabled test may need the app's settings patched instead of an env var. If so, set `client.app.state.settings.worker.skill_overrides_enabled = False` inside the test rather than `monkeypatch.setenv`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_prompts_api.py -k skill_overrides -v`
Expected: FAIL — `KeyError: 'skill_overrides'` (not yet stored).

- [ ] **Step 3: Store the overrides in the config build**

In `_submit_one`, locate the existing config build (`surogates/api/routes/prompts.py:176`):

```python
config: dict = {
    "service_account_id": str(service_account_id),
}
if body.metadata:
    config["pipeline_metadata"] = body.metadata
```

Add directly after it:

```python
worker_settings = getattr(request.app.state.settings, "worker", request.app.state.settings)
if body.skill_overrides:
    if getattr(worker_settings, "skill_overrides_enabled", True):
        config["skill_overrides"] = {
            name: ov.model_dump(exclude_none=False)
            for name, ov in body.skill_overrides.items()
        }
    else:
        logger.warning(
            "skill_overrides supplied but feature flag disabled; dropping "
            "(org=%s, keys=%s)",
            tenant.org_id, sorted(body.skill_overrides),
        )
```

(`request`, `tenant`, `logger`, `service_account_id` are already in scope in `_submit_one` — confirm `logger` exists at module top; if not, add `logger = logging.getLogger(__name__)`.)

The service-account gate is already enforced: the `/v1/api/prompts` route calls `_require_service_account(tenant)` before `_submit_one`, so a JWT caller can never reach this code with `skill_overrides`. Add a regression test asserting that to be explicit:

```python
async def test_skill_overrides_rejected_for_jwt_caller(client, session_factory):
    # A user JWT (not a service-account token) must be 403 on /v1/api/prompts.
    _org_id, user_jwt = await _non_admin_tenant(session_factory)
    resp = await client.post(
        "/v1/api/prompts",
        json={"prompt": "hi", "skill_overrides": {"x": {"content": "y"}}},
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert resp.status_code == 403
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_prompts_api.py -k skill_overrides -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/api/routes/prompts.py tests/integration/test_prompts_api.py
git commit -m "feat(prompts): persist service-account skill_overrides to session.config"
```

---

## Task 4: Loader override layer (`_apply_overrides` + `load_skills(overrides=)`)

**Files:**
- Modify: `surogates/tools/loader.py:195` (`load_skills`), add `_apply_overrides`
- Test: `tests/test_loader_overrides.py`

This is the core. `_apply_overrides` field-merges the candidate onto the matching original `SkillDef` via `dataclasses.replace`, preserving `source` so downstream staging still resolves the original supporting files.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_loader_overrides.py
from dataclasses import replace

from surogates.tools.loader import (
    ResourceLoader,
    SkillDef,
    SKILL_SOURCE_PLATFORM,
)


def _skill(name, content, source=SKILL_SOURCE_PLATFORM, **kw):
    return SkillDef(name=name, description=f"{name} desc", content=content,
                    source=source, **kw)


def test_apply_overrides_patches_content_preserving_source():
    base = [
        _skill("browser-research", "ORIGINAL", category="web", trigger="orig"),
        _skill("other", "KEEP"),
    ]
    overrides = {
        "browser-research": {
            "content": "CANDIDATE",
            "description": "new desc",
            "trigger": "new trigger",
        }
    }
    out = {s.name: s for s in ResourceLoader._apply_overrides(base, overrides)}

    patched = out["browser-research"]
    assert patched.content == "CANDIDATE"
    assert patched.description == "new desc"
    assert patched.trigger == "new trigger"
    # source + category preserved so staging still resolves original files.
    assert patched.source == SKILL_SOURCE_PLATFORM
    assert patched.category == "web"
    # unrelated skills untouched.
    assert out["other"].content == "KEEP"


def test_apply_overrides_partial_fields_fall_back_to_base():
    base = [_skill("s", "ORIGINAL", trigger="orig-trigger")]
    overrides = {"s": {"content": "CANDIDATE"}}  # no description/trigger
    out = ResourceLoader._apply_overrides(base, overrides)[0]
    assert out.content == "CANDIDATE"
    assert out.description == "s desc"           # fell back to base
    assert out.trigger == "orig-trigger"          # fell back to base


def test_apply_overrides_ignores_when_base_missing():
    base = [_skill("present", "X")]
    overrides = {"ghost": {"content": "NEW", "description": "d", "trigger": "t"}}
    out = {s.name: s for s in ResourceLoader._apply_overrides(base, overrides)}
    assert "ghost" not in out
    assert out["present"].content == "X"


def test_apply_overrides_empty_is_noop():
    base = [_skill("s", "X")]
    assert ResourceLoader._apply_overrides(base, {}) == base
    assert ResourceLoader._apply_overrides(base, None) == base
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_loader_overrides.py -v`
Expected: FAIL — `AttributeError: type object 'ResourceLoader' has no attribute '_apply_overrides'`.

- [ ] **Step 3: Add `_apply_overrides` and the `overrides=` param**

At the top of `surogates/tools/loader.py`, ensure `from dataclasses import replace` is imported (add if missing).

Add this static method to `ResourceLoader` (next to `_merge` near line 723):

```python
@staticmethod
def _apply_overrides(
    skills: list["SkillDef"],
    overrides: dict[str, dict] | None,
) -> list["SkillDef"]:
    """Apply session-scoped candidate overrides as the top layer.

    For each ``{name: {content, description?, trigger?, ...}}`` entry,
    field-merge the candidate onto the matching ``SkillDef`` via
    ``dataclasses.replace`` so ``source``/``category``/expert fields are
    preserved — keeping supporting-file staging pointed at the original
    skill source.  A name with no matching base is ignored; SkillOpt
    optimizes an existing bound skill and must not inject a new session-only
    command that is not present in the target agent's catalog.

    Returns a NEW list; the input is not mutated.  ``None``/empty
    ``overrides`` returns the input list unchanged (identity).
    """
    if not overrides:
        return skills
    by_name: dict[str, SkillDef] = {s.name: s for s in skills}
    for name, ov in overrides.items():
        content = ov.get("content")
        if not content:
            continue
        base = by_name.get(name)
        if base is None:
            continue
        by_name[name] = replace(
            base,
            content=content,
            description=ov.get("description") or base.description,
            trigger=ov.get("trigger") if ov.get("trigger") is not None else base.trigger,
        )
    return list(by_name.values())
```

Now thread `overrides` through `load_skills`. Change its signature (line 195) to add the parameter:

```python
async def load_skills(
    self,
    tenant: Any,
    db_session: Any | None = None,
    bundle: Any | None = None,
    system_bundle: Any | None = None,
    overrides: dict[str, dict] | None = None,
) -> list[SkillDef]:
```

At every `return self._merge(...)` inside `load_skills` (there are two: the db-session branch and the fallback branch), wrap the result:

```python
        return self._apply_overrides(
            self._merge(platform, user_files, org_db, user_db), overrides,
        )
```

and for the fallback:

```python
    return self._apply_overrides(self._merge(platform, user_files), overrides)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_loader_overrides.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the existing loader suite to confirm no regression**

Run: `uv run pytest tests/test_loader.py tests/test_loader_overrides.py tests/test_loader_skills_system_bundle.py tests/runtime/test_resource_loader_bundle.py -q`
Expected: PASS (override param defaults to `None`, so existing behavior is unchanged).

- [ ] **Step 6: Commit**

```bash
git add surogates/tools/loader.py tests/test_loader_overrides.py
git commit -m "feat(loader): add session-scoped skill override layer"
```

---

## Task 5: API `view_skill` resolves overrides (shared-runtime path)

**Files:**
- Modify: `surogates/api/routes/skills.py:432` (`view_skill`), add `_load_session_skill_overrides` helper
- Test: `tests/integration/test_skills_overrides.py`

`view_skill` already accepts `session_id`. We load that session's overrides and pass them to `load_skills`. Because `_apply_overrides` preserves `source`, the existing `linked_files` enumeration and `_stage_skill_for_session` call below it continue to stage the *original* supporting files; only `content` is swapped.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_skills_overrides.py
# Reuse the app/client + service-account fixture pattern from
# tests/integration/test_skills_sa_token.py.

async def test_view_skill_returns_override_content(
    client, session_factory, seed_platform_skill, make_session_with_overrides
):
    # A published platform skill "browser-research" exists in the bundle.
    seed_platform_skill("browser-research", body="ORIGINAL BODY")
    sid = await make_session_with_overrides(
        skill="browser-research", content="CANDIDATE BODY",
    )

    resp = await client.get(
        f"/v1/skills/browser-research?session_id={sid}",
        headers=_service_account_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "CANDIDATE BODY" in body["content"]
    assert "ORIGINAL BODY" not in body["content"]


async def test_view_skill_without_session_is_unchanged(
    client, seed_platform_skill
):
    seed_platform_skill("browser-research", body="ORIGINAL BODY")
    resp = await client.get(
        "/v1/skills/browser-research", headers=_service_account_headers(),
    )
    assert resp.status_code == 200
    assert "ORIGINAL BODY" in resp.json()["content"]
```

Implement `seed_platform_skill` and `make_session_with_overrides` in this test file. `seed_platform_skill` writes a `skills/<name>/SKILL.md` into the test bundle or platform-skill fixture used by the app. `make_session_with_overrides` creates a session row with `config={"skill_overrides": {skill: {"content": content}}}` via `session_store`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_skills_overrides.py -v`
Expected: FAIL — override content not returned (original body comes back).

- [ ] **Step 3: Add the override-load helper**

In `surogates/api/routes/skills.py`, add near the other private helpers (above `view_skill`):

```python
async def _load_session_skill_overrides(
    request: Request,
    tenant: "TenantContext",
    session_id: "UUID | None",
) -> dict[str, dict]:
    """Return the session's ``skill_overrides`` map, or ``{}``.

    Returns ``{}`` when no session is given, the feature flag is off, the
    session does not belong to the tenant, or the session carries no
    overrides.  Authorization reuses the same tenant-ownership check the
    staging path uses, so a caller cannot read another tenant's
    overrides by guessing a session id.
    """
    if session_id is None:
        return {}
    worker_settings = getattr(request.app.state.settings, "worker", request.app.state.settings)
    if not getattr(worker_settings, "skill_overrides_enabled", True):
        return {}
    try:
        session = await _authorize_session_for_staging(request, tenant, session_id)
    except HTTPException:
        return {}
    return (session.config or {}).get("skill_overrides") or {}
```

- [ ] **Step 4: Wire it into `view_skill`**

In `view_skill`, replace the existing skills-load block (the `async with session_factory()` that calls `loader.load_skills(...)`) so it passes overrides:

```python
    overrides = await _load_session_skill_overrides(request, tenant, session_id)
    async with session_factory() as db_session:
        all_skills = await loader.load_skills(
            tenant,
            db_session=db_session,
            bundle=bundle,
            system_bundle=system_bundle,
            overrides=overrides,
        )
```

Leave the rest of `view_skill` unchanged — `skill_def = next(... name ...)` now yields the patched def (override content, original source), so `detail.content`, `linked_files`, and `_stage_skill_for_session` all behave correctly.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_skills_overrides.py -v`
Expected: PASS.

- [ ] **Step 6: Add a staging-isolation test**

```python
async def test_override_stages_original_supporting_files(
    client, session_factory, seed_platform_skill_with_files,
    make_session_with_overrides, read_staged_file,
):
    # Skill has SKILL.md + scripts/run.py in the bundle.
    seed_platform_skill_with_files(
        "browser-research", body="ORIGINAL",
        files={"scripts/run.py": "print('original script')"},
    )
    sid = await make_session_with_overrides(
        skill="browser-research", content="CANDIDATE",
    )
    resp = await client.get(
        f"/v1/skills/browser-research?session_id={sid}",
        headers=_service_account_headers(),
    )
    assert resp.status_code == 200
    assert "CANDIDATE" in resp.json()["content"]
    # The staged script is the ORIGINAL, not affected by the override.
    assert "original script" in await read_staged_file(sid, "browser-research", "scripts/run.py")
```

Run: `uv run pytest tests/integration/test_skills_overrides.py -v`
Expected: PASS (content overridden, files original).

- [ ] **Step 7: Commit**

```bash
git add surogates/api/routes/skills.py tests/integration/test_skills_overrides.py
git commit -m "feat(skills-api): view_skill resolves session skill overrides"
```

---

## Task 6: API `list_skills` honors overrides (catalog completeness)

**Files:**
- Modify: `surogates/api/routes/skills.py:382` (`list_skills`)
- Test: `tests/integration/test_skills_overrides.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_list_skills_reflects_override_description(
    client, seed_platform_skill, make_session_with_overrides
):
    seed_platform_skill("browser-research", body="ORIGINAL", description="old desc")
    sid = await make_session_with_overrides(
        skill="browser-research", content="CANDIDATE", description="new desc",
    )
    resp = await client.get(
        f"/v1/skills?session_id={sid}", headers=_service_account_headers(),
    )
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert by_name["browser-research"]["description"] == "new desc"
```

> `make_session_with_overrides` must accept `description=` and store it in the override entry.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_skills_overrides.py -k list_skills -v`
Expected: FAIL — old description returned.

- [ ] **Step 3: Add `session_id` param + override load to `list_skills`**

Change the `list_skills` signature to accept the session id:

```python
@read_router.get("/skills", response_model=SkillListResponse)
async def list_skills(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    type: str | None = None,
    session_id: UUID | None = None,
) -> SkillListResponse:
```

(Ensure `from uuid import UUID` is imported in this module — `view_skill` already uses it.)

Replace the load block to pass overrides:

```python
    overrides = await _load_session_skill_overrides(request, tenant, session_id)
    async with session_factory() as db_session:
        all_skills = await loader.load_skills(
            tenant,
            db_session=db_session,
            bundle=bundle,
            system_bundle=system_bundle,
            overrides=overrides,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/integration/test_skills_overrides.py -k list_skills -v`
Expected: PASS.

- [ ] **Step 5: Confirm the catalog-browsing mount is unaffected**

```python
async def test_v1_api_mount_ignores_overrides_without_session(
    client, seed_platform_skill,
):
    # The website catalog mount (/v1/api/skills) without session_id is unchanged.
    seed_platform_skill("browser-research", body="ORIGINAL", description="old desc")
    resp = await client.get("/v1/api/skills", headers=_service_account_headers())
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert by_name["browser-research"]["description"] == "old desc"
```

Run: `uv run pytest tests/integration/test_skills_overrides.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add surogates/api/routes/skills.py tests/integration/test_skills_overrides.py
git commit -m "feat(skills-api): list_skills honors session overrides via session_id"
```

---

## Task 7: `HarnessAPIClient.list_skills` forwards `session_id`

**Files:**
- Modify: `surogates/harness/api_client.py:20` (`list_skills`)
- Test: `tests/test_harness_api_client.py`

`view_skill` already forwards `session_id`. Make `list_skills` do the same so the worker-side catalog (used by slash expansion) sees overrides.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness_api_client.py
import json
import httpx
import pytest

from surogates.harness.api_client import HarnessAPIClient


@pytest.mark.asyncio
async def test_list_skills_forwards_session_id():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"skills": []})

    transport = httpx.MockTransport(handler)
    client = HarnessAPIClient(base_url="http://api", token="t", session_id="sess-1")
    client._client = httpx.AsyncClient(transport=transport, base_url="http://api",
                                       headers={"Authorization": "Bearer t"})
    await client.list_skills()
    assert "session_id=sess-1" in captured["url"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_api_client.py -v`
Expected: FAIL — `session_id` not in the request URL.

- [ ] **Step 3: Forward `session_id` in `list_skills`**

In `surogates/harness/api_client.py`, update `list_skills`:

```python
async def list_skills(self, category: str | None = None) -> str:
    """List available skills.  Returns JSON string for tool result."""
    params: dict[str, Any] = {}
    if category:
        params["category"] = category
    if self._session_id is not None:
        params["session_id"] = self._session_id
    data = await self._get("/v1/skills", params=params or None)
    skills = data.get("skills", [])
    return json.dumps({
        "success": True,
        "skills": skills,
        "categories": sorted(set(s.get("category") for s in skills if s.get("category"))),
        "count": len(skills),
        "hint": "Use skill_view(name) to see full content, tags, and linked files",
    }, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness_api_client.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/api_client.py tests/test_harness_api_client.py
git commit -m "feat(api-client): forward session_id on list_skills for override catalog"
```

---

## Task 8: Slash `/<skill>` expansion uses override content (coverage)

**Files:**
- Test only: `tests/test_slash_skill.py`

No production change is expected: `expand_slash_skill` → `_expand_skill` → `skill_view(name, session_id)`, and Task 5 made `view_skill` return override content. This task adds a regression test proving the end-to-end path so a future refactor can't silently break it.

- [ ] **Step 1: Write the test**

```python
# tests/test_slash_skill.py
import pytest

from surogates.harness.slash_skill import expand_slash_skill


@pytest.mark.asyncio
async def test_slash_expansion_uses_override_content(
    fake_tools, tenant_context, override_api_client,
):
    # override_api_client.view_skill returns candidate content for the
    # overridden skill (simulating the API server with session overrides).
    result = await expand_slash_skill(
        text="/browser-research compare vendors",
        tools=fake_tools,
        tenant=tenant_context,
        session_id="sess-1",
        api_client=override_api_client,
        session_factory=None,
    )
    assert result is not None
    expanded_text, name, staged_at, kind = result
    assert name == "browser-research"
    assert kind == "skill"
    assert "CANDIDATE BODY" in expanded_text
```

Build `override_api_client` as a small fake exposing `async def view_skill(self, name, file_path=None)` returning `json.dumps({"success": True, "content": "CANDIDATE BODY", "name": name})` and `async def list_skills(self, category=None)` returning a catalog JSON containing `browser-research` as a non-expert skill (so `_load_skills_for_slash` resolves it and branches to `_expand_skill`).

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_slash_skill.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_slash_skill.py
git commit -m "test(slash): cover override content in /<skill> expansion"
```

---

## Task 9: Audit — enrich `skill.invoked` when an override was used

**Files:**
- Modify: `surogates/harness/loop.py:968` (the `skill.invoked` emit block)
- Test: `tests/test_loop_skill_invoked_override.py`

Per the spec's Auditability section, the event must record `override_source`, `skillopt_run_id`, and `candidate_id` when the session carried an override for the invoked skill.

- [ ] **Step 1: Write the failing test**

Extract the metadata-building into a pure helper and cover it with focused unit tests:

```python
# tests/test_loop_skill_invoked_override.py
from surogates.harness.loop import _skill_invoked_event_data


def test_event_data_plain():
    data = _skill_invoked_event_data(
        skill_name="browser-research",
        raw_message="/browser-research x",
        staged_at="/ws/.skills/browser-research",
        session_config={},
    )
    assert data == {
        "skill": "browser-research",
        "raw_message": "/browser-research x",
        "staged_at": "/ws/.skills/browser-research",
    }


def test_event_data_with_override():
    cfg = {"skill_overrides": {"browser-research": {
        "content": "...", "source": "skillopt",
        "run_id": "run-1", "candidate_id": "cand-2",
    }}}
    data = _skill_invoked_event_data(
        skill_name="browser-research",
        raw_message="/browser-research x",
        staged_at=None,
        session_config=cfg,
    )
    assert data["override_source"] == "skillopt"
    assert data["skillopt_run_id"] == "run-1"
    assert data["candidate_id"] == "cand-2"


def test_event_data_no_override_for_this_skill():
    cfg = {"skill_overrides": {"other": {"content": "...", "run_id": "r"}}}
    data = _skill_invoked_event_data(
        skill_name="browser-research", raw_message="/x", staged_at=None,
        session_config=cfg,
    )
    assert "override_source" not in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_loop_skill_invoked_override.py -v`
Expected: FAIL — `ImportError: cannot import name '_skill_invoked_event_data'`.

- [ ] **Step 3: Add the helper and use it in the emit block**

In `surogates/harness/loop.py`, add a module-level pure function:

```python
def _skill_invoked_event_data(
    *,
    skill_name: str,
    raw_message: str,
    staged_at: str | None,
    session_config: dict | None,
) -> dict:
    """Build the ``skill.invoked`` event payload, tagging override use.

    When the session config carries a ``skill_overrides`` entry for the
    invoked skill, the SkillOpt run/candidate ids are added so rollouts
    can be joined back to candidates in observability.
    """
    data: dict = {
        "skill": skill_name,
        "raw_message": raw_message,
        "staged_at": staged_at,
    }
    ov = ((session_config or {}).get("skill_overrides") or {}).get(skill_name)
    if ov:
        data["override_source"] = ov.get("source", "skillopt")
        if ov.get("run_id") is not None:
            data["skillopt_run_id"] = ov["run_id"]
        if ov.get("candidate_id") is not None:
            data["candidate_id"] = ov["candidate_id"]
    return data
```

In the emit block (`loop.py:968`), replace the inline dict passed to `emit_event(... EventType.SKILL_INVOKED ...)`:

```python
                    await self._store.emit_event(
                        session.id,
                        EventType.SKILL_INVOKED,
                        _skill_invoked_event_data(
                            skill_name=skill_name,
                            raw_message=last_user_content,
                            staged_at=staged_at,
                            session_config=session.config,
                        ),
                    )
```

(`session` is in scope in the turn loop; `session.config` is the JSONB dict.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_loop_skill_invoked_override.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add surogates/harness/loop.py tests/test_loop_skill_invoked_override.py
git commit -m "feat(loop): tag skill.invoked events with override provenance"
```

---

## Task 10: Worker-local `skill_view` applies overrides (dedicated-agent path)

**Files:**
- Modify: `surogates/tools/builtin/skills.py:156` (`_load_all_skills`) and `:292` (`_skill_view_handler`)
- Test: `tests/test_builtin_skills_overrides.py`

Shared-runtime sessions go through the API path (Tasks 5–7). Dedicated (helm) agents run tools worker-locally with `api_client is None`. For those, the override must be applied in `_load_all_skills`. Normal tool dispatch already passes `session_config=session.config`; slash expansion must pass that same config through in local mode.

- [ ] **Step 1: Confirm what the dispatcher passes**

Run: `rg -n "session_config=session.config|session_config\": session.config|expand_slash_skill\\(" surogates/harness surogates/tools`

Expected: normal tool dispatch in `surogates/harness/tool_exec.py` already passes `session_config=session.config`. `expand_slash_skill(...)` in `surogates/harness/loop.py` does not yet pass `session_config`, so local slash catalog loading cannot see overrides.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_builtin_skills_overrides.py
import json
import pytest

from surogates.tools.builtin.skills import _skill_view_handler


@pytest.mark.asyncio
async def test_worker_local_skill_view_applies_override(
    tenant_context, tmp_platform_skill,  # writes SKILL.md "ORIGINAL" to disk
):
    out = await _skill_view_handler(
        {"name": "browser-research"},
        tenant=tenant_context,
        api_client=None,
        session_config={
            "skill_overrides": {
                "browser-research": {"content": "CANDIDATE BODY"},
            },
        },
    )
    payload = json.loads(out)
    assert "CANDIDATE BODY" in payload["content"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_builtin_skills_overrides.py -v`
Expected: FAIL — original content returned.

- [ ] **Step 4: Thread `session_config["skill_overrides"]` through `_load_all_skills`**

Update `_load_all_skills` (`skills.py:156`) to read overrides from kwargs and pass them to the loader:

```python
async def _load_all_skills(tenant: Any, **kwargs: Any) -> list:
    """Load skills from all layers, using DB when a session factory is available."""
    from surogates.tools.loader import ResourceLoader

    settings = _settings_from_kwargs(kwargs)
    loader = ResourceLoader.from_settings(settings)
    session_config = kwargs.get("session_config") or {}
    overrides = kwargs.get("skill_overrides") or session_config.get("skill_overrides")
    worker_settings = getattr(settings, "worker", settings)
    if not getattr(worker_settings, "skill_overrides_enabled", True):
        overrides = None
    session_factory = kwargs.get("session_factory")
    if session_factory is not None:
        async with session_factory() as db_session:
            return await loader.load_skills(
                tenant, db_session=db_session, overrides=overrides,
            )
    return await loader.load_skills(tenant, overrides=overrides)
```

`_skill_view_handler`'s worker-local branch calls `_load_all_skills(**kwargs)`, so passing `session_config` in kwargs is enough — the override SkillDef (with original source) is returned and the handler serves its content. No further change to `_skill_view_handler` is required beyond ensuring it forwards `**kwargs` (it already does).

- [ ] **Step 5: Pass `session_config` through local slash expansion**

Change `expand_slash_skill` in `surogates/harness/slash_skill.py` to accept and forward `session_config`:

```python
async def expand_slash_skill(
    *,
    text: str,
    tools: Any,
    tenant: Any,
    session_id: str,
    api_client: Any | None,
    session_factory: Any | None,
    session_config: dict[str, Any] | None = None,
    session_store: Any | None = None,
    sandbox_pool: Any | None = None,
) -> tuple[str, str, str | None, Literal["skill", "expert"]] | None:
```

Pass it to catalog loading:

```python
        catalog = await _load_skills_for_slash(
            tenant,
            api_client=api_client,
            session_factory=session_factory,
            session_config=session_config,
        )
```

And pass it into `skill_view` dispatch:

```python
        result = await tools.dispatch(
            "skill_view",
            {"name": name},
            tenant=tenant,
            session_id=session_id,
            api_client=api_client,
            session_factory=session_factory,
            session_config=session_config,
        )
```

In `surogates/harness/loop.py`, add the new argument at the `expand_slash_skill(...)` call:

```python
                    session_config=session.config,
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_builtin_skills_overrides.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add surogates/tools/builtin/skills.py surogates/harness/slash_skill.py surogates/harness/loop.py tests/test_builtin_skills_overrides.py
git commit -m "feat(tools): apply skill overrides in worker-local skill_view"
```

---

## Task 11: Full-suite verification

- [ ] **Step 1: Run the touched suites**

Run:
```bash
uv run pytest tests/integration/test_prompts_api.py tests/test_prompts_schema.py \
  tests/integration/test_skills_overrides.py tests/test_loader_overrides.py \
  tests/test_builtin_skills_overrides.py \
  tests/test_harness_api_client.py tests/test_slash_skill.py \
  tests/test_loop_skill_invoked_override.py tests/test_worker_settings_skill_overrides.py -q
```
Expected: all PASS.

- [ ] **Step 2: Run the broader skill/loader/prompts areas for regressions**

Run: `uv run pytest tests/test_loader.py tests/test_loader_overrides.py tests/test_skill_view_handler.py tests/test_builtin_skills_overrides.py tests/test_harness_api_client.py tests/test_slash_skill.py tests/integration/test_prompts_api.py tests/integration/test_skills_overrides.py tests/integration/test_skills_sa_token.py -q`
Expected: PASS. Investigate any failure before proceeding — `overrides` defaults to `None`/`{}` everywhere, so a failure indicates a wiring mistake.

- [ ] **Step 3: Final commit if anything was adjusted**

```bash
git add -A && git commit -m "test(skill-overrides): full-suite verification"
```

---

## Self-Review notes (verify during execution)

- **Spec coverage:** prompt-API validation+storage (Tasks 1,3), service-account gate (Task 3), precedence as highest layer (Task 4), `skill_view` override (Task 5), `list_skills` unchanged for catalog mount (Task 6), slash expansion (Task 8), auditability metadata (Task 9), security/isolation — overrides never written to catalogs and authorized per-session (Tasks 3,5). Staging-from-original-source isolation (Task 5 step 6).
- **Type consistency:** `overrides: dict[str, dict] | None` is the single shape passed everywhere (`load_skills`, `_apply_overrides`, `_load_session_skill_overrides`). The Pydantic `SkillOverride` is serialized to a plain dict via `model_dump` at storage time (Task 3), so all downstream consumers see dicts, never the Pydantic type.
- **Edge:** an override whose `content` is empty is skipped by `_apply_overrides` (Task 4) — defensive against a malformed candidate.
