# Arbor Research Missions v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One real arbor cycle end to end — `/auto-research` creates a research-kind mission whose strict coordinator grows a DB-backed Idea Tree, dispatches executor task-workers into git worktrees, harvests results deterministically at wake, and merges into trunk only through a machine-run held-out eval gate.

**Architecture:** Per the spec at `docs/superpowers/specs/2026-06-12-arbor-research-missions-design.md` (§4): two new sidecar tables (`research_runs`, `idea_nodes`; no `missions` ALTER), a `surogates/arbor/` package (store + propagate + prompts + evaluator policy), three HARNESS builtin tools (`idea_tree`, `dispatch_experiments`, `merge_experiment`), a pre-LLM harvest mixin modeled on `BoardMixin`, a research-kind branch in the mission evaluator hook, and the `/auto-research` slash command. Judgment ships as skills; guarantees live in tools.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async (`Mapped`/`mapped_column`, Postgres JSONB), Pydantic v2, pytest + pytest-asyncio + testcontainers (fixtures `engine` / `session_factory` / `session_store` / `redis_client` in `tests/integration/conftest.py`).

**Read first:** the spec §§4.1–4.6 (architecture + control flow), `surogates/missions/` (the pattern this follows), `surogates/harness/loop_board.py` (the mixin pattern), `study/Arbor/src/coordinator/idea_tree.py` and `tools/git_ops.py` (the semantics being ported).

**Conventions (hard rules):**
- Run tests with `pytest` from `/work/surogates` (NOT `uv run` — it clobbers the local dev install).
- Commit messages: conventional (`feat:`/`test:`/`refactor:`), NO task/step numbers, NO Co-Authored-By trailers.
- New builtin tools MUST get `TOOL_LOCATIONS` entries + a routing regression test (platform rule — unlisted tools silently route to the sandbox and fail as "Unknown tool").
- Pure-unit tests go flat in `tests/`; DB-backed tests in `tests/integration/`.

---

## File structure

```
surogates/arbor/__init__.py            # empty package marker
surogates/arbor/models.py              # Pydantic shapes: ExperimentReport, NodeStatus literals
surogates/arbor/store.py               # ResearchStore: run/node CRUD, gates data, constraints block, meta writes
surogates/arbor/propagate.py           # deterministic concat-propagate (LLM synthesis lands in v2)
surogates/arbor/prompts.py             # executor brief, kickoff, continuation, leaderboard block builders
surogates/arbor/evaluator_policy.py    # research-kind judge policy (skip-in-flight, verified satisfied, demotions)
surogates/tools/builtin/arbor.py       # the three tools: schemas, handlers, register()
surogates/harness/loop_arbor.py        # ArborHarvestMixin (pre-LLM fold, fail-open)
surogates/tasks/service.py             # create_task_and_spawn() factored from _spawn_task_handler

Modified:
surogates/db/models.py                 # +ResearchRun, +IdeaNode (new tables only)
surogates/tools/router.py              # +3 TOOL_LOCATIONS HARNESS entries
surogates/tools/runtime.py             # +arbor module in register_builtins
surogates/orchestrator/worker.py       # _filter_effective_tools: research-tool visibility gate
surogates/tools/loader.py              # AgentDef.preloaded_skills field + frontmatter key
surogates/tasks/spawn.py               # honor agent_def.preloaded_skills in _build_task_worker_config
surogates/tasks/tools.py               # _spawn_task_handler delegates to tasks/service.py
surogates/missions/commands.py         # parse_auto_research_command + handle_research_mission_create
surogates/harness/slash_skill.py       # +"auto-research" in _BUILTIN_SLASH_COMMANDS
surogates/harness/loop.py              # 3 touches: slash match (~924), harvest hook (~1304), filter branch (~3029)
surogates/harness/loop_mission_evaluator.py  # research-kind dispatch after the active-mission fetch
surogates/session/events.py            # +research.* EventType members

Skills (hub bundles):
skills/research/arbor-research/SKILL.md
skills/research/arbor-coordinator/SKILL.md
skills/research/arbor-executor/SKILL.md

Tests:
tests/test_arbor_routing.py            # TOOL_LOCATIONS + registration + visibility regression
tests/test_arbor_parse.py              # /auto-research parsing
tests/test_arbor_propagate.py          # concat-propagate unit tests
tests/test_arbor_evaluator_policy.py   # judge-policy unit tests
tests/test_arbor_harvest.py            # harvest fold unit tests (stubbed store)
tests/integration/test_arbor_store.py  # tables + store CRUD + gates data + meta guards
tests/integration/test_arbor_tools.py  # dispatch/merge gates with a fake sandbox + fake spawn
```

Dependency order: Task 1 → 2 → 3 are the data spine; 4 unlocks 6–7; 5 is independent; 8–11 wire the loop; 12–13 close out. Implement in order.

---

### Task 1: DB tables — `ResearchRun` and `IdeaNode`

**Files:**
- Modify: `surogates/db/models.py` (append after the `Mission` class, ~line 1230)
- Test: `tests/integration/test_arbor_store.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_arbor_store.py
"""Integration tests for the Arbor research tables and ResearchStore."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from surogates.db.models import IdeaNode, ResearchRun


@pytest.mark.asyncio(loop_scope="session")
async def test_research_tables_roundtrip(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    async with session_factory() as db:
        run = ResearchRun(
            org_id=org_id, mission_id=mission_id, session_id=session_id,
            agent_id="agent-x", repo_path="/workspace/repo",
            trunk_branch="research/trunk", branch_prefix="research/run1",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        root = IdeaNode(
            org_id=org_id, run_id=run.id, node_key="ROOT",
            parent_key=None, depth=0, hypothesis="(objective)",
        )
        db.add(root)
        await db.commit()

    async with session_factory() as db:
        got = (await db.execute(
            select(IdeaNode).where(IdeaNode.run_id == run.id)
        )).scalars().all()
        assert [n.node_key for n in got] == ["ROOT"]
        assert got[0].status == "pending"
        assert run.status == "init"
        assert run.meta == {}


@pytest.mark.asyncio(loop_scope="session")
async def test_idea_node_key_unique_per_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    async with session_factory() as db:
        run = ResearchRun(
            org_id=org_id, mission_id=mission_id, session_id=session_id,
            agent_id="agent-x", repo_path="/workspace/repo",
            trunk_branch="t", branch_prefix="p",
        )
        db.add(run)
        await db.commit()
        db.add(IdeaNode(org_id=org_id, run_id=run.id, node_key="1",
                        parent_key="ROOT", depth=1, hypothesis="h"))
        await db.commit()
        db.add(IdeaNode(org_id=org_id, run_id=run.id, node_key="1",
                        parent_key="ROOT", depth=1, hypothesis="dup"))
        with pytest.raises(Exception):  # IntegrityError via uq constraint
            await db.commit()
```

Add this fixture to `tests/integration/conftest.py` (so later arbor test files reuse it; the existing conftest seeds inbox principals but not missions — note it must be `pytest_asyncio.fixture`, not `pytest.fixture`, because `loop_scope` is a pytest-asyncio kwarg):

```python
@pytest_asyncio.fixture(loop_scope="session")
async def seeded_org_and_session(session_factory):
    """Insert minimal org + user + session + mission rows; return their ids."""
    from surogates.db.models import Mission, Org, Session, User

    org_id, user_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with session_factory() as db:
        db.add(Org(id=org_id, name=f"org-{org_id.hex[:6]}"))
        db.add(User(id=user_id, org_id=org_id,
                    email=f"u-{user_id.hex[:6]}@x.test", password_hash="x"))
        await db.commit()
        db.add(Session(id=session_id, org_id=org_id, user_id=user_id,
                       agent_id="agent-x", config={}))
        await db.commit()
        m = Mission(org_id=org_id, user_id=user_id, session_id=session_id,
                    agent_id="agent-x", description="d", rubric="r")
        db.add(m)
        await db.commit()
        await db.refresh(m)
    return org_id, m.id, session_id
```

NOTE: check the exact required columns of `Org`/`User`/`Session` in `surogates/db/models.py` while implementing — if a NOT NULL column is missing from the seeder, add it with a dummy value. Mirror how `tests/integration` seeds principals elsewhere if a helper already exists (search for `Org(` in `tests/integration/` first and reuse it if found).

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_arbor_store.py -x -q`
Expected: `ImportError: cannot import name 'ResearchRun'`

- [ ] **Step 3: Add the two ORM models**

Append to `surogates/db/models.py` after `Mission`, matching its style exactly (same imports already present in the module: `Mapped`, `mapped_column`, `UUID`, `Text`, `Integer`, `Float`, `ForeignKey`, `Index`, `CheckConstraint`, `UniqueConstraint`, JSONB, `func`):

```python
class ResearchRun(Base):
    """Sidecar row that makes a mission a research (Arbor) run.

    Presence of this row IS the research-kind dispatch — ``missions``
    itself is never altered. ``meta`` mirrors Arbor's ``tree.meta``
    (closed key set, enforced by ResearchStore; machine-score keys are
    writable only by merge/baseline paths).
    """

    __tablename__ = "research_runs"
    __table_args__ = (
        Index("idx_research_runs_session", "session_id"),
        UniqueConstraint("mission_id", name="uq_research_runs_mission"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    trunk_branch: Mapped[str] = mapped_column(Text, nullable=False)
    branch_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="init",
    )
    meta: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )


class IdeaNode(Base):
    """One hypothesis in a research run's Idea Tree.

    ``node_key`` is Arbor's dotted-decimal key ("ROOT", "1", "1.2").
    ``score`` is the absolute dev-split score (never a delta).
    ``task_id`` is the experiment ledger join: dispatch writes it,
    harvest folds by it.
    """

    __tablename__ = "idea_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "node_key", name="uq_idea_nodes_run_key"),
        Index("idx_idea_nodes_run_status", "run_id", "status"),
        CheckConstraint(
            "status IN ('pending','running','done','failed','merged','pruned')",
            name="ck_idea_nodes_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_runs.id"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(Text, nullable=False)
    parent_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending",
    )
    insight: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    code_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_work: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )
```

If any of `DateTime`, `Float`, `UniqueConstraint`, `JSONB`, `Optional` are not already imported at the top of `models.py`, extend the existing import lines (do not add duplicate import blocks).

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_arbor_store.py -x -q`
Expected: `2 passed` (tables arrive via the suite's `Base.metadata.create_all`)

- [ ] **Step 5: Commit**

```bash
git add surogates/db/models.py tests/integration/test_arbor_store.py
git commit -m "feat: add research_runs and idea_nodes tables for arbor research missions"
```

---

### Task 2: `surogates/arbor/` package — models + `ResearchStore`

**Files:**
- Create: `surogates/arbor/__init__.py` (empty), `surogates/arbor/models.py`, `surogates/arbor/store.py`
- Test: extend `tests/integration/test_arbor_store.py`

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/test_arbor_store.py`)

```python
from surogates.arbor.store import (
    MetaKeyError, NodeStateError, ResearchStore,
)


@pytest_asyncio.fixture(loop_scope="session")
async def research_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/trunk", branch_prefix="research/run1",
        objective="maximize F1",
    )
    return store, run_id, org_id


@pytest.mark.asyncio(loop_scope="session")
async def test_create_run_seeds_root_and_defaults(research_run):
    store, run_id, _ = research_run
    run = await store.get_run(run_id)
    assert run.status == "init"
    assert run.meta["metric_direction"] == "maximize"
    assert run.meta["max_cycles"] == 20
    root = await store.get_node(run_id, "ROOT")
    assert root.depth == 0 and root.status == "pending"


@pytest.mark.asyncio(loop_scope="session")
async def test_set_meta_enforces_closed_keys_and_machine_keys(research_run):
    store, run_id, _ = research_run
    await store.set_meta(run_id, {"eval_cmd": "python eval.py --split dev"})
    with pytest.raises(MetaKeyError):
        await store.set_meta(run_id, {"not_a_real_key": 1})
    with pytest.raises(MetaKeyError):  # machine-score keys rejected from the LLM path
        await store.set_meta(run_id, {"test_trunk_score": 99.0})
    await store.set_meta(
        run_id, {"test_trunk_score": 99.0}, allow_machine_keys=True,
    )
    assert (await store.get_run(run_id)).meta["test_trunk_score"] == 99.0


@pytest.mark.asyncio(loop_scope="session")
async def test_add_update_and_cycle_accounting(research_run):
    store, run_id, org_id = research_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT",
                         hypothesis="Mechanism: X\nHypothesis: Y\nObservable: Z\nConflicts: none")
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h2")
    nodes = await store.list_nodes(run_id)
    keys = sorted(n.node_key for n in nodes if n.node_key != "ROOT")
    assert keys == ["1", "2"]
    assert await store.cycles_spent(run_id) == 0
    await store.update_node(run_id, "1", status="running")
    await store.update_node(run_id, "1", status="done", score=0.41,
                            insight="works", result="ok")
    await store.update_node(run_id, "2", status="failed",
                            insight="Timed out")
    assert await store.cycles_spent(run_id) == 2  # done + failed both spend


@pytest.mark.asyncio(loop_scope="session")
async def test_prune_is_recursive_and_terminal(research_run):
    store, run_id, org_id = research_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h1")
    await store.add_node(run_id, org_id=org_id, parent_key="1", hypothesis="child")
    await store.prune(run_id, "1", reason="dead end")
    n1 = await store.get_node(run_id, "1")
    child = await store.get_node(run_id, "1.1")
    assert n1.status == "pruned" and child.status == "pruned"
    assert "[Pruned: dead end]" in (n1.insight or "")
    with pytest.raises(NodeStateError):
        await store.update_node(run_id, "1", status="running")


def test_is_improvement_direction_aware():
    from surogates.arbor.store import is_improvement
    assert is_improvement(0.5, 0.4, "maximize")
    assert not is_improvement(0.3, 0.4, "maximize")
    assert is_improvement(0.3, 0.4, "minimize")
    assert is_improvement(0.5, None, "maximize")  # no baseline yet → improvement
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/integration/test_arbor_store.py -x -q`
Expected: `ModuleNotFoundError: No module named 'surogates.arbor'`

- [ ] **Step 3: Implement `surogates/arbor/models.py`**

```python
"""Pydantic shapes shared by the arbor tools, harvest hook, and evaluator."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

NodeStatus = Literal["pending", "running", "done", "failed", "merged", "pruned"]

TERMINAL_NODE_STATUSES: tuple[str, ...] = ("done", "failed", "merged", "pruned")
BUDGET_SPENDING_STATUSES: tuple[str, ...] = ("done", "failed", "merged", "pruned")


class ExperimentReport(BaseModel):
    """Structured extraction target for an executor's free-text report.

    Used by the harvest hook only when the worker forgot to put the
    fields in ``worker_complete(metadata=...)``.
    """

    node_key: str = ""
    score: float | None = None
    insight: str = ""
    result: str = ""
    branch: str = ""
```

- [ ] **Step 4: Implement `surogates/arbor/store.py`**

Port semantics from `study/Arbor/src/coordinator/idea_tree.py` (key allocation :28-120, meta keys :124-141, `is_improvement` :193-198). Follow `surogates/missions/store.py` for the class shape.

```python
"""DB CRUD for research runs and idea nodes.

Mirrors MissionStore's shape. All meta writes are per-key
``jsonb_set`` UPDATEs — never read-modify-write of the whole blob.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.arbor.models import BUDGET_SPENDING_STATUSES
from surogates.db.models import IdeaNode, ResearchRun

# Closed meta key set, ported from Arbor's tree.meta (idea_tree.py:124-141)
# plus the run-config keys the spec adds (§4.3).
META_KEYS: frozenset[str] = frozenset({
    "objective",
    "baseline_score", "trunk_score",
    "test_baseline_score", "test_trunk_score",
    "eval_cmd", "eval_cmd_test", "eval_timeout",
    "eval_retries", "eval_retry_base_delay", "eval_retry_max_delay",
    "metric_direction", "dataset_info",
    "protected_paths", "required_outputs",
    "max_cycles", "max_tree_depth", "max_parallel",
    "merge_threshold", "hitl_mode",
    "convergence_window", "convergence_min_delta",
    "merge_eval",
})

# Writable ONLY by merge_experiment / the baseline-record path
# (allow_machine_keys=True). idea_tree(set_meta) cannot fake progress.
MACHINE_KEYS: frozenset[str] = frozenset({
    "test_baseline_score", "test_trunk_score", "trunk_score", "merge_eval",
})

DEFAULT_META: dict[str, Any] = {
    "metric_direction": "maximize",
    "max_cycles": 20,
    "max_tree_depth": 3,
    "max_parallel": 2,
    "merge_threshold": 0.0,
    "eval_timeout": 1800,
    "eval_retries": 1,
    "eval_retry_base_delay": 10,
    "eval_retry_max_delay": 60,
    "hitl_mode": "auto",
}

_MUTABLE_NODE_FIELDS = frozenset({
    "status", "score", "insight", "result", "code_ref",
    "related_work", "task_id", "dispatched_at", "completed_at",
})
_TERMINAL = frozenset({"merged", "pruned"})


class ResearchStoreError(Exception):
    """Base for research store errors."""


class MetaKeyError(ResearchStoreError):
    """Unknown meta key, or machine key written from the LLM path."""


class NodeStateError(ResearchStoreError):
    """Illegal node state transition (e.g. mutating a pruned node)."""


def is_improvement(
    candidate: float | None, reference: float | None, direction: str,
) -> bool:
    """Direction-aware comparison (port of idea_tree.py:193-198)."""
    if candidate is None:
        return False
    if reference is None:
        return True
    if direction == "minimize":
        return candidate < reference
    return candidate > reference


class ResearchStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def create_run(
        self, *, org_id: UUID, mission_id: UUID, session_id: UUID,
        agent_id: str, repo_path: str, trunk_branch: str,
        branch_prefix: str, objective: str,
        meta_overrides: dict[str, Any] | None = None,
    ) -> UUID:
        meta = dict(DEFAULT_META)
        meta["objective"] = objective
        for k, v in (meta_overrides or {}).items():
            if k not in META_KEYS:
                raise MetaKeyError(f"unknown meta key: {k}")
            meta[k] = v
        async with self._sf() as db:
            run = ResearchRun(
                org_id=org_id, mission_id=mission_id, session_id=session_id,
                agent_id=agent_id, repo_path=repo_path,
                trunk_branch=trunk_branch, branch_prefix=branch_prefix,
                meta=meta,
            )
            db.add(run)
            await db.flush()
            db.add(IdeaNode(
                org_id=org_id, run_id=run.id, node_key="ROOT",
                parent_key=None, depth=0, hypothesis=objective,
            ))
            await db.commit()
            return run.id

    async def get_run(self, run_id: UUID) -> ResearchRun:
        async with self._sf() as db:
            run = await db.get(ResearchRun, run_id)
            if run is None:
                raise ResearchStoreError(f"research run {run_id} not found")
            db.expunge(run)
            return run

    async def get_run_for_mission(self, mission_id: UUID) -> ResearchRun | None:
        async with self._sf() as db:
            run = await db.scalar(
                select(ResearchRun).where(ResearchRun.mission_id == mission_id)
            )
            if run is not None:
                db.expunge(run)
            return run

    async def get_run_for_session(self, session_id: UUID) -> ResearchRun | None:
        async with self._sf() as db:
            run = await db.scalar(
                select(ResearchRun)
                .where(ResearchRun.session_id == session_id)
                .order_by(ResearchRun.created_at.desc())
                .limit(1)
            )
            if run is not None:
                db.expunge(run)
            return run

    async def set_run_status(self, run_id: UUID, status: str) -> None:
        async with self._sf() as db:
            await db.execute(
                update(ResearchRun)
                .where(ResearchRun.id == run_id)
                .values(status=status)
            )
            await db.commit()

    async def set_meta(
        self, run_id: UUID, values: dict[str, Any],
        *, allow_machine_keys: bool = False,
    ) -> None:
        """Per-key jsonb_set writes — no read-modify-write race surface."""
        for key in values:
            if key not in META_KEYS:
                raise MetaKeyError(f"unknown meta key: {key}")
            if key in MACHINE_KEYS and not allow_machine_keys:
                raise MetaKeyError(
                    f"meta key {key!r} is machine-written only "
                    "(merge_experiment / baseline path)"
                )
        async with self._sf() as db:
            for key, value in values.items():
                await db.execute(
                    update(ResearchRun)
                    .where(ResearchRun.id == run_id)
                    .values(meta=func.jsonb_set(
                        ResearchRun.meta, f"{{{key}}}",
                        func.cast(func.to_jsonb(value), JSONB),
                        True,
                    ))
                )
            await db.commit()

    async def add_node(
        self, run_id: UUID, *, org_id: UUID, parent_key: str, hypothesis: str,
    ) -> IdeaNode:
        """Allocate the next dotted-decimal child key under parent_key."""
        async with self._sf() as db:
            parent = await db.scalar(
                select(IdeaNode).where(
                    IdeaNode.run_id == run_id, IdeaNode.node_key == parent_key,
                )
            )
            if parent is None:
                raise ResearchStoreError(f"parent node {parent_key!r} not found")
            if parent.status in _TERMINAL:
                raise NodeStateError(f"parent {parent_key!r} is {parent.status}")
            prefix = "" if parent_key == "ROOT" else f"{parent_key}."
            siblings = (await db.execute(
                select(IdeaNode.node_key).where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.parent_key == parent_key,
                )
            )).scalars().all()
            next_ordinal = 1 + max(
                (int(k.rsplit(".", 1)[-1]) for k in siblings), default=0,
            )
            node = IdeaNode(
                org_id=org_id, run_id=run_id,
                node_key=f"{prefix}{next_ordinal}",
                parent_key=parent_key,
                depth=1 if parent_key == "ROOT" else parent.depth + 1,
                hypothesis=hypothesis,
            )
            db.add(node)
            await db.commit()
            await db.refresh(node)
            db.expunge(node)
            return node

    async def get_node(self, run_id: UUID, node_key: str) -> IdeaNode:
        async with self._sf() as db:
            node = await db.scalar(
                select(IdeaNode).where(
                    IdeaNode.run_id == run_id, IdeaNode.node_key == node_key,
                )
            )
            if node is None:
                raise ResearchStoreError(f"node {node_key!r} not found")
            db.expunge(node)
            return node

    async def list_nodes(self, run_id: UUID) -> list[IdeaNode]:
        async with self._sf() as db:
            nodes = (await db.execute(
                select(IdeaNode)
                .where(IdeaNode.run_id == run_id)
                .order_by(IdeaNode.node_key)
            )).scalars().all()
            for n in nodes:
                db.expunge(n)
            return list(nodes)

    async def update_node(
        self, run_id: UUID, node_key: str, **fields: Any,
    ) -> None:
        unknown = set(fields) - _MUTABLE_NODE_FIELDS
        if unknown:
            raise ResearchStoreError(f"immutable/unknown node fields: {unknown}")
        async with self._sf() as db:
            node = await db.scalar(
                select(IdeaNode).where(
                    IdeaNode.run_id == run_id, IdeaNode.node_key == node_key,
                )
            )
            if node is None:
                raise ResearchStoreError(f"node {node_key!r} not found")
            if node.status in _TERMINAL and fields.get("status") != node.status:
                raise NodeStateError(
                    f"node {node_key!r} is terminal ({node.status})"
                )
            for k, v in fields.items():
                setattr(node, k, v)
            if fields.get("status") in ("done", "failed", "merged"):
                node.completed_at = node.completed_at or datetime.now(timezone.utc)
            await db.commit()

    async def prune(self, run_id: UUID, node_key: str, reason: str) -> list[str]:
        """Recursively prune node_key and its subtree. Returns pruned keys."""
        async with self._sf() as db:
            nodes = (await db.execute(
                select(IdeaNode).where(IdeaNode.run_id == run_id)
            )).scalars().all()
            by_key = {n.node_key: n for n in nodes}
            if node_key not in by_key:
                raise ResearchStoreError(f"node {node_key!r} not found")
            doomed = [k for k in by_key
                      if k == node_key or k.startswith(node_key + ".")]
            for k in doomed:
                n = by_key[k]
                if n.status in _TERMINAL:
                    continue
                n.status = "pruned"
                tag = f"[Pruned: {reason}]"
                n.insight = f"{n.insight}\n{tag}" if n.insight else tag
            await db.commit()
            return doomed

    async def cycles_spent(self, run_id: UUID) -> int:
        async with self._sf() as db:
            return int(await db.scalar(
                select(func.count(IdeaNode.id)).where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.status.in_(BUDGET_SPENDING_STATUSES),
                    IdeaNode.node_key != "ROOT",
                )
            ) or 0)

    async def in_flight_count(self, run_id: UUID) -> int:
        async with self._sf() as db:
            return int(await db.scalar(
                select(func.count(IdeaNode.id)).where(
                    IdeaNode.run_id == run_id,
                    IdeaNode.status == "running",
                )
            ) or 0)
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/integration/test_arbor_store.py -x -q`
Expected: all pass. If `func.to_jsonb` fails on your SQLAlchemy version, replace the jsonb_set expression with `text("jsonb_set(meta, :path, to_jsonb(:val::text)::jsonb, true)")` bound params — verify with the test, don't guess.

- [ ] **Step 6: Commit**

```bash
git add surogates/arbor/ tests/integration/test_arbor_store.py
git commit -m "feat: add arbor ResearchStore with closed meta keys and cycle accounting"
```

---

### Task 3: Constraints block + concat-propagate

**Files:**
- Create: `surogates/arbor/propagate.py`
- Modify: `surogates/arbor/store.py` (add `constraints_block` + `render_markdown`)
- Test: `tests/test_arbor_propagate.py` (new, pure unit) + extend `tests/integration/test_arbor_store.py`

- [ ] **Step 1: Write the failing unit test**

```python
# tests/test_arbor_propagate.py
"""Unit tests for deterministic insight concat-propagation."""
from surogates.arbor.propagate import concat_propagate


def test_concat_propagate_appends_child_lesson_up_the_chain():
    insights = {"ROOT": "objective", "1": None, "1.1": "lr=3e-4 overfits"}
    parents = {"1": "ROOT", "1.1": "1"}
    updates = concat_propagate(
        node_key="1.1", insight="lr=3e-4 overfits",
        insights=insights, parents=parents, cap_chars=1200,
    )
    assert updates["1"] == "[from 1.1] lr=3e-4 overfits"
    assert "[from 1.1] lr=3e-4 overfits" in updates["ROOT"]


def test_concat_propagate_caps_and_keeps_tail():
    insights = {"ROOT": "x" * 1190, "1": "p"}
    parents = {"1": "ROOT"}
    updates = concat_propagate(
        node_key="1", insight="LESSON",
        insights=insights, parents=parents, cap_chars=1200,
    )
    assert len(updates["ROOT"]) <= 1200
    assert updates["ROOT"].endswith("[from 1] LESSON")


def test_concat_propagate_skips_empty_insight():
    assert concat_propagate(
        node_key="1", insight="", insights={"ROOT": None, "1": None},
        parents={"1": "ROOT"}, cap_chars=1200,
    ) == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_arbor_propagate.py -x -q`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `surogates/arbor/propagate.py`**

```python
"""Deterministic insight propagation (crash-safe; no LLM in the wake path).

LLM-synthesis backprop (the verbatim port of Arbor tree_ops.py:555-571)
arrives in v2 inside tool-call paths; this concat pass keeps the
constraints block fresh even when every LLM call fails.
"""
from __future__ import annotations


def concat_propagate(
    *, node_key: str, insight: str,
    insights: dict[str, str | None],
    parents: dict[str, str],
    cap_chars: int = 1200,
) -> dict[str, str]:
    """Append "[from <key>] <insight>" to every ancestor's insight.

    Returns {ancestor_key: new_insight}; caller persists. Keeps the
    TAIL when over cap — the newest lessons matter most.
    """
    insight = (insight or "").strip()
    if not insight:
        return {}
    stamp = f"[from {node_key}] {insight}"
    updates: dict[str, str] = {}
    current = parents.get(node_key)
    while current is not None:
        existing = (insights.get(current) or "").strip()
        merged = f"{existing}\n{stamp}" if existing else stamp
        if len(merged) > cap_chars:
            merged = merged[-cap_chars:]
        updates[current] = merged
        insights[current] = merged
        current = parents.get(current)
    return updates
```

- [ ] **Step 4: Add `constraints_block` + `render_markdown` to `ResearchStore`** (append methods)

```python
    async def constraints_block(self, run_id: UUID) -> str:
        """The anti-amnesia artifact, re-read every IDEATE.

        Port of idea_tree.py:358-435 — TREE SHAPE / ROOT INSIGHT /
        PRUNED LESSONS / VALIDATED FINDINGS over the DB rows.
        """
        run = await self.get_run(run_id)
        nodes = await self.list_nodes(run_id)
        by_key = {n.node_key: n for n in nodes}
        meta = run.meta or {}
        lines: list[str] = ["## RESEARCH CONSTRAINTS", "", "### TREE SHAPE"]
        for n in sorted(nodes, key=lambda x: ([0] if x.node_key == "ROOT" else [1], x.node_key)):
            indent = "  " * (0 if n.node_key == "ROOT" else n.depth)
            score = f" score={n.score}" if n.score is not None else ""
            first_line = (n.hypothesis or "").splitlines()[0][:100]
            lines.append(f"{indent}- {n.node_key} [{n.status}]{score} {first_line}")
        root = by_key.get("ROOT")
        lines += ["", "### ROOT INSIGHT",
                  (root.insight if root and root.insight else "(none yet)")]
        pruned = [n for n in nodes if n.status == "pruned" and n.insight]
        lines += ["", "### PRUNED LESSONS"]
        lines += [f"- {n.node_key}: {n.insight.splitlines()[-1][:200]}"
                  for n in pruned] or ["(none)"]
        merged = [n for n in nodes if n.status == "merged"]
        lines += ["", "### VALIDATED FINDINGS"]
        lines += [f"- {n.node_key} score={n.score}: {(n.insight or '')[:200]}"
                  for n in merged] or ["(none)"]
        lines += [
            "", "### BUDGET & DISCIPLINE",
            f"- cycles: {await self.cycles_spent(run_id)}/{meta.get('max_cycles')}"
            f" | depth cap: {meta.get('max_tree_depth')}"
            f" | max parallel: {meta.get('max_parallel')}",
            f"- scores: baseline={meta.get('baseline_score')}"
            f" trunk(dev)={meta.get('trunk_score')}"
            f" test_baseline={meta.get('test_baseline_score')}"
            f" test_trunk={meta.get('test_trunk_score')}"
            f" ({meta.get('metric_direction')})",
            "- B_dev for iteration; B_test ONLY through merge_experiment.",
        ]
        return "\n".join(lines)
```

Add an integration test asserting the block contains `### TREE SHAPE`, the node line for `"1"`, and the budget line after the Task 2 fixtures ran (8-line test, same file).

- [ ] **Step 5: Run both test files; commit**

Run: `pytest tests/test_arbor_propagate.py tests/integration/test_arbor_store.py -q`
Expected: all pass.

```bash
git add surogates/arbor/ tests/
git commit -m "feat: add constraints block rendering and concat insight propagation"
```

---

### Task 4: Tool plumbing — `idea_tree` tool, routing, registration, visibility

**Files:**
- Create: `surogates/tools/builtin/arbor.py`
- Modify: `surogates/tools/router.py:107` (before the SANDBOX section), `surogates/tools/runtime.py` (~line 99, after `board_tools`), `surogates/orchestrator/worker.py::_filter_effective_tools`
- Test: `tests/test_arbor_routing.py` (new)

- [ ] **Step 1: Write the failing routing regression test**

```python
# tests/test_arbor_routing.py
"""Routing + registration + visibility regression for the arbor tools.

Platform rule: a builtin tool missing from TOOL_LOCATIONS silently
routes to the sandbox executor and fails as 'Unknown tool'.
"""
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation

ARBOR_TOOLS = ("idea_tree", "dispatch_experiments", "merge_experiment")


def test_arbor_tools_route_to_harness():
    for name in ARBOR_TOOLS:
        assert TOOL_LOCATIONS.get(name) == ToolLocation.HARNESS, (
            f"{name} must have an explicit HARNESS entry in TOOL_LOCATIONS"
        )


def test_arbor_tools_register():
    from surogates.tools.builtin import arbor
    registered: list[str] = []

    class FakeRegistry:
        def register(self, *, name, schema, handler, toolset="core", **kw):
            registered.append(name)

    arbor.register(FakeRegistry())
    assert sorted(registered) == sorted(ARBOR_TOOLS)


def test_arbor_tools_hidden_without_active_run():
    from surogates.orchestrator.worker import _filter_effective_tools
    # Coordinator session WITHOUT a research run: tools stripped.
    tools = list(ARBOR_TOOLS) + ["spawn_task"]
    filtered = _filter_effective_tools(
        tools, session_config={"coordinator": True}, task_id=None,
        has_api_client=True, context_group_id=None,
    )
    assert not set(ARBOR_TOOLS) & set(filtered)
    # Research coordinator: tools visible.
    filtered = _filter_effective_tools(
        tools,
        session_config={"coordinator": True,
                        "active_research_run_id": "r1"},
        task_id=None, has_api_client=True, context_group_id=None,
    )
    assert set(ARBOR_TOOLS) <= set(filtered)
    # Task workers NEVER see them (executors stay tree-blind).
    filtered = _filter_effective_tools(
        tools,
        session_config={"active_research_run_id": "r1"},
        task_id="t1", has_api_client=True, context_group_id=None,
    )
    assert not set(ARBOR_TOOLS) & set(filtered)
```

IMPORTANT: `_filter_effective_tools`'s real signature in `surogates/orchestrator/worker.py:121-204` differs from this sketch — read it first and adapt the test calls to the actual parameters (it derives flags from the session row/config). Keep the three assertions exactly; change only the call shape.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_arbor_routing.py -x -q`
Expected: FAIL on the first assert (no TOOL_LOCATIONS entries)

- [ ] **Step 3: Add routing entries** in `surogates/tools/router.py` immediately before the `# Sandbox (code execution, ...)` comment:

```python
    # Arbor research tools — handlers need the DB session factory,
    # the LLM client, and the sandbox pool (server-side git ops);
    # none of that exists in a sandbox pod.
    "idea_tree": ToolLocation.HARNESS,
    "dispatch_experiments": ToolLocation.HARNESS,
    "merge_experiment": ToolLocation.HARNESS,
```

- [ ] **Step 4: Create `surogates/tools/builtin/arbor.py`** with schemas, the `idea_tree` handler (all non-LLM actions), stub handlers for the other two (filled in Tasks 6–7), and `register()`:

```python
"""Arbor research tools: idea_tree, dispatch_experiments, merge_experiment.

Deterministic spine of research missions (spec §4.4). Judgment lives in
the arbor-* skills; these handlers enforce the guarantees: closed meta
keys, dispatch gates, and the no-score-argument merge gate.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from surogates.tools.registry import ToolSchema

logger = logging.getLogger(__name__)

_IDEA_TREE_SCHEMA = ToolSchema(
    name="idea_tree",
    description=(
        "Read and mutate this research run's Idea Tree. Actions: "
        "view (format=constraints|compact), add(parent_key, hypothesis), "
        "update(node_key, fields), prune(node_key, reason), "
        "set_meta(values), record_from_task(task_id), "
        "requeue(node_key, reason), report."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "view", "add", "update", "prune", "set_meta",
                "record_from_task", "requeue", "report",
            ]},
            "format": {"type": "string", "enum": ["constraints", "compact"]},
            "parent_key": {"type": "string"},
            "node_key": {"type": "string"},
            "hypothesis": {"type": "string"},
            "fields": {"type": "object"},
            "values": {"type": "object"},
            "task_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action"],
    },
)

_FOUR_LINE_MARKERS = ("Mechanism:", "Hypothesis:", "Observable:", "Conflicts:")


def _hypothesis_warnings(hypothesis: str) -> list[str]:
    """Machine-warn on non-4-line hypotheses (port of arbor_state.py
    validate_hypothesis — warn, never block; quality is the skill's job)."""
    missing = [m for m in _FOUR_LINE_MARKERS if m not in hypothesis]
    if missing:
        return [f"hypothesis missing {', '.join(missing)} — use the 4-line format"]
    return []


async def _require_run(session_config: dict, session_factory: Any):
    from surogates.arbor.store import ResearchStore
    raw = (session_config or {}).get("active_research_run_id")
    if not raw:
        raise ValueError("no active research run on this session")
    return ResearchStore(session_factory), UUID(str(raw))


async def _idea_tree_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    store, run_id = await _require_run(
        kwargs.get("session_config") or {}, kwargs["session_factory"],
    )
    run = await store.get_run(run_id)
    action = arguments["action"]

    if action == "view":
        if arguments.get("format") == "compact":
            nodes = await store.list_nodes(run_id)
            rows = [
                f"{n.node_key}\t{n.status}\t{n.score if n.score is not None else '-'}"
                f"\t{(n.hypothesis or '').splitlines()[0][:80]}"
                for n in nodes
            ]
            return "key\tstatus\tscore\thypothesis\n" + "\n".join(rows)
        return await store.constraints_block(run_id)

    if action == "add":
        warnings = _hypothesis_warnings(arguments.get("hypothesis") or "")
        meta = run.meta or {}
        parent = await store.get_node(run_id, arguments["parent_key"])
        if parent.node_key != "ROOT" and parent.depth >= int(meta.get("max_tree_depth", 3)):
            return json.dumps({"error": (
                f"depth cap {meta.get('max_tree_depth')} reached at "
                f"{parent.node_key}; refine an existing branch or prune"
            )})
        node = await store.add_node(
            run_id, org_id=run.org_id,
            parent_key=arguments["parent_key"],
            hypothesis=arguments["hypothesis"],
        )
        out = {"node_key": node.node_key, "depth": node.depth}
        if warnings:
            out["warnings"] = warnings
        return json.dumps(out)

    if action == "update":
        fields = dict(arguments.get("fields") or {})
        fields.pop("score", None)  # scores arrive via harvest/merge, not prose
        await store.update_node(run_id, arguments["node_key"], **fields)
        return json.dumps({"ok": True})

    if action == "prune":
        pruned = await store.prune(
            run_id, arguments["node_key"], arguments.get("reason") or "no reason",
        )
        return json.dumps({"pruned": pruned})

    if action == "set_meta":
        from surogates.arbor.store import MetaKeyError
        try:
            await store.set_meta(run_id, dict(arguments.get("values") or {}))
        except MetaKeyError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps({"ok": True})

    if action == "record_from_task":
        return await _record_from_task(
            store, run_id, arguments["task_id"], kwargs,
        )

    if action == "requeue":
        node = await store.get_node(run_id, arguments["node_key"])
        if node.status not in ("done", "failed"):
            return json.dumps({"error": f"cannot requeue a {node.status} node"})
        await store.update_node(
            run_id, arguments["node_key"], status="pending", task_id=None,
        )
        return json.dumps({
            "ok": True,
            "note": "requeued; the spent cycle is NOT refunded "
                    f"(reason: {arguments.get('reason') or 'unspecified'})",
        })

    if action == "report":
        # Lazy import — prompts.py (incl. build_report) is created with
        # dispatch_experiments; no earlier test exercises this action.
        from surogates.arbor.prompts import build_report
        report = build_report(run, await store.list_nodes(run_id))
        await _persist_workspace_file(
            kwargs, path=".arbor/REPORT.md", content=report,
        )
        return report

    return json.dumps({"error": f"unknown action {action!r}"})


async def _record_from_task(store, run_id, task_id: str, kwargs) -> str:
    """Coordinator's correction channel: fold a Task row by id —
    reads result/result_metadata from the DB, never coordinator prose."""
    from sqlalchemy import select
    from surogates.db.models import Task

    async with kwargs["session_factory"]() as db:
        task = await db.get(Task, UUID(task_id))
        if task is None:
            return json.dumps({"error": f"task {task_id} not found"})
    # Lazy import — loop_arbor.py is created in a later change; this
    # action is only exercised once harvest exists.
    from surogates.harness.loop_arbor import fold_task_into_node
    folded = await fold_task_into_node(
        store, run_id, task, llm_client=None, model=None,
    )
    return json.dumps(folded)


async def _persist_workspace_file(kwargs, *, path: str, content: str) -> None:
    """Write a file under /workspace via the session's sandbox."""
    pool = kwargs.get("sandbox_pool")
    if pool is None:
        return
    from surogates.sandbox.base import default_sandbox_spec
    owner = str(kwargs["session_id"])
    await pool.ensure(owner, default_sandbox_spec())
    await pool.execute(owner, "write_file", json.dumps({
        "path": f"/workspace/{path}", "content": content,
    }))


# dispatch_experiments / merge_experiment get real schemas + handlers in
# their own tasks; minimal stubs keep register() valid until then.
_DISPATCH_SCHEMA = ToolSchema(
    name="dispatch_experiments", description="(implemented later)",
    parameters={"type": "object", "properties": {}},
)
_MERGE_SCHEMA = ToolSchema(
    name="merge_experiment", description="(implemented later)",
    parameters={"type": "object", "properties": {}},
)


async def _dispatch_experiments_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"error": "not implemented"})


async def _merge_experiment_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    return json.dumps({"error": "not implemented"})


def register(registry) -> None:
    """Register arbor tools. Called once per registry by tools/runtime.py."""
    registry.register(
        name="idea_tree", schema=_IDEA_TREE_SCHEMA,
        handler=_idea_tree_handler, toolset="core",
    )
    registry.register(
        name="dispatch_experiments", schema=_DISPATCH_SCHEMA,
        handler=_dispatch_experiments_handler, toolset="core",
    )
    registry.register(
        name="merge_experiment", schema=_MERGE_SCHEMA,
        handler=_merge_experiment_handler, toolset="core",
    )
```

The stub schemas/handlers above are replaced in the dispatch and merge tasks. The `ToolSchema(name, description, parameters)` envelope and `registry.register(name=, schema=, handler=, toolset=)` signature are verified against `surogates/tools/registry.py:22-39` and `surogates/board/tools.py:31,384-403`; handler shape is `async def handler(arguments: dict, **kwargs) -> str`.

- [ ] **Step 5: Register the module** in `surogates/tools/runtime.py` — add to the import list next to `board_tools`:

```python
        from surogates.tools.builtin import arbor as arbor_tools
```

and append `arbor_tools,  # idea_tree, dispatch_experiments, merge_experiment (research missions)` to the modules list.

- [ ] **Step 6: Add the visibility gate** in `surogates/orchestrator/worker.py::_filter_effective_tools`, beside the existing task-self-tools block:

```python
    # Arbor research tools: coordinator-only, and only while a research
    # run is active. Executors stay tree-blind (mle_kaggle.yaml:41-46 —
    # no second shared-state protocol between executors).
    _ARBOR_TOOLS = ("idea_tree", "dispatch_experiments", "merge_experiment")
    is_research_coordinator = (
        bool(config.get("active_research_run_id")) and task_id is None
    )
    if not is_research_coordinator:
        tools = [t for t in tools if t not in _ARBOR_TOOLS]
```

Adapt variable names (`config`, `task_id`, `tools`) to the function's actual locals — read it first.

- [ ] **Step 7: Run the tests; commit**

Run: `pytest tests/test_arbor_routing.py -x -q`
Expected: 3 passed.

```bash
git add surogates/tools/ surogates/orchestrator/worker.py tests/test_arbor_routing.py
git commit -m "feat: add arbor builtin tools with HARNESS routing and coordinator-only visibility"
```

---

### Task 5: `create_task_and_spawn` factoring + `AgentDef.preloaded_skills`

**Files:**
- Create: `surogates/tasks/service.py`
- Modify: `surogates/tasks/tools.py:299` (`_spawn_task_handler` delegates), `surogates/tools/loader.py` (AgentDef field + frontmatter key), `surogates/tasks/spawn.py:84-87` (honor the field)
- Test: extend `tests/test_arbor_routing.py`? No — new `tests/test_task_service.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_task_service.py
"""Unit tests for the factored task-spawn service + AgentDef skill preload."""
from surogates.tasks.spawn import _build_task_worker_config
from surogates.tools.loader import AgentDef


def _agent_def(**kw):
    return AgentDef(
        name="arbor-executor", description="d", system_prompt="body",
        source="platform", **kw,
    )


class _Task:
    agent_def_name = "arbor-executor"


def test_agent_def_preloaded_skills_reach_worker_config():
    cfg = _build_task_worker_config(
        _agent_def(preloaded_skills=["arbor-executor"]), _Task(),
    )
    assert "arbor-executor" in cfg["preloaded_skills"]
    assert "subagent-task-worker" in cfg["preloaded_skills"]


def test_no_preloaded_skills_keeps_default_only():
    cfg = _build_task_worker_config(_agent_def(), _Task())
    assert cfg["preloaded_skills"] == ["subagent-task-worker"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_task_service.py -x -q`
Expected: `TypeError: AgentDef.__init__() got an unexpected keyword argument 'preloaded_skills'`

- [ ] **Step 3: Implement**

(a) `surogates/tools/loader.py` — add to the `AgentDef` dataclass after `tags`:

```python
    preloaded_skills: list[str] | None = None  # skills inlined into the worker's system prompt
```

and add `"preloaded_skills"` to `_KNOWN_AGENT_FRONTMATTER_KEYS` (loader.py:67-72) plus the frontmatter→field assignment where the other list fields (`tools`, `disallowed_tools`) are parsed — follow the existing list-coercion helper in that parser.

(b) `surogates/tasks/spawn.py::_build_task_worker_config` — replace the trailing preload block (spawn.py:84-87) with:

```python
    preloaded = list(cfg.get("preloaded_skills") or [])
    if agent_def is not None and agent_def.preloaded_skills:
        for s in agent_def.preloaded_skills:
            if s not in preloaded:
                preloaded.append(s)
    if "subagent-task-worker" not in preloaded:
        preloaded.append("subagent-task-worker")
    cfg["preloaded_skills"] = preloaded
```

(c) `surogates/tasks/service.py` — extract the insert+eager-spawn core of `_spawn_task_handler` (tasks/tools.py:299) into:

```python
"""Programmatic task creation — shared by the spawn_task LLM tool and
dispatch_experiments (research missions)."""
from __future__ import annotations

from typing import Any
from uuid import UUID


async def create_task_and_spawn(
    *,
    goal: str,
    agent_def_name: str | None,
    max_attempts: int,
    mission_id: UUID | None,
    parent_session_id: UUID,
    org_id: UUID,
    session_factory: Any,
    session_store: Any,
    tenant: Any,
    redis: Any,
) -> UUID:
    """Insert a Task row and eagerly spawn its first attempt.

    This is a refactor seam: move the existing row-insert +
    eager-spawn lines out of ``_spawn_task_handler`` VERBATIM (same
    defaults, same event emissions, same group inheritance via
    ``ensure_group_and_inherit``) and have both callers share it.
    The handler keeps its argument parsing, sibling checks, and reply
    formatting; this function owns insert + spawn only.
    """
    ...
```

Implementation rule: this is a refactor, not new behavior — lift the exact statements from `_spawn_task_handler` (read tasks/tools.py:299-460 first; keep `ensure_group_and_inherit`, the `task.created` event, and the dispatcher enqueue exactly as they are). Then make `_spawn_task_handler` call it. The existing task-layer tests in `tests/tasks/` are the regression net for this step — run them.

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_task_service.py tests/tasks/ -q`
Expected: new tests pass; ALL existing task-layer tests still pass (the factoring must be behavior-preserving).

- [ ] **Step 5: Commit**

```bash
git add surogates/tasks/ surogates/tools/loader.py tests/test_task_service.py
git commit -m "refactor: factor create_task_and_spawn and add AgentDef.preloaded_skills"
```

---

### Task 6: `dispatch_experiments` — gates, server-side worktrees, executor briefs

**Files:**
- Create: `surogates/arbor/prompts.py`
- Modify: `surogates/tools/builtin/arbor.py` (real schema + handler)
- Test: `tests/integration/test_arbor_tools.py` (new)

- [ ] **Step 1: Write the failing gate tests** (fake sandbox + fake spawner so no K8s/git needed)

```python
# tests/integration/test_arbor_tools.py
"""Dispatch and merge gate tests with a fake sandbox pool."""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from surogates.tools.builtin.arbor import (
    _dispatch_experiments_handler,
)


class FakeSandboxPool:
    """Records exec calls; scripted stdout per command substring."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self.responses = responses or {}

    async def ensure(self, session_id, spec):
        return session_id

    async def execute(self, session_id, name, input):
        self.calls.append((session_id, name, input))
        for needle, out in self.responses.items():
            if needle in input:
                return out
        return ""


class _StubSessionStore:
    async def emit_event(self, *a, **kw):
        return None


@pytest_asyncio.fixture(loop_scope="session")
async def dispatch_env(session_factory, seeded_org_and_session):
    from surogates.arbor.store import ResearchStore

    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/run1/trunk", branch_prefix="research/run1",
        objective="maximize F1",
        meta_overrides={"max_cycles": 2, "max_parallel": 1},
    )
    await store.set_meta(run_id, {
        "eval_cmd": "python eval.py --split dev",
        "eval_cmd_test": "python eval.py --split test",
    })
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h1")
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h2")
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": session_id,
        "sandbox_pool": FakeSandboxPool(),
        "session_store": _StubSessionStore(),
        "tenant": object(),
        "redis": object(),
    }
    return store, run_id, kwargs["session_config"], kwargs


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_refuses_when_budget_spent(dispatch_env):
    store, run_id, session_config, kwargs = dispatch_env
    await store.update_node(run_id, "1", status="done", insight="i")
    await store.update_node(run_id, "2", status="failed", insight="t")
    # cycles_spent == max_cycles == 2 → refuse
    out = json.loads(await _dispatch_experiments_handler(
        {"node_keys": ["1"]}, **kwargs,
    ))
    assert "budget" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_refuses_non_pending_and_non_leaf(dispatch_env):
    store, run_id, session_config, kwargs = dispatch_env
    await store.update_node(run_id, "1", status="running")
    out = json.loads(await _dispatch_experiments_handler(
        {"node_keys": ["1"]}, **kwargs,
    ))
    assert "pending" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_creates_worktree_brief_and_task(dispatch_env):
    store, run_id, session_config, kwargs = dispatch_env
    with patch(
        "surogates.tasks.service.create_task_and_spawn",
        new=AsyncMock(return_value=uuid.uuid4()),
    ) as spawn:
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": ["1"]}, **kwargs,
        ))
    assert out["dispatched"] == ["1"]
    node = await store.get_node(run_id, "1")
    assert node.status == "running" and node.task_id is not None
    assert node.code_ref and node.code_ref.startswith("research/")
    # worktree created server-side BEFORE the worker's first token
    pool: FakeSandboxPool = kwargs["sandbox_pool"]
    assert any("git worktree add" in i for (_, _, i) in pool.calls)
    # brief: eval_cmd present, the eval_cmd_test COMMAND never rendered
    brief = spawn.call_args.kwargs["goal"]
    assert "eval.py --split dev" in brief
    assert "eval.py --split test" not in brief
```

The handler kwarg names in `dispatch_env` must match what `tool_exec.py:590-657` actually passes to HARNESS handlers — verify there before finalizing (the fixture above uses the names confirmed from the coordinator/tasks handler registrations).

- [ ] **Step 2: Run to verify failure** — `pytest tests/integration/test_arbor_tools.py -x -q` → stub handler returns `not implemented`.

- [ ] **Step 3: Implement `surogates/arbor/prompts.py::build_executor_brief`** (port of `executor_run.py:248-365`; STRICTER than native — `eval_cmd_test` is never rendered):

```python
"""Prompt builders for research missions (briefs, kickoff, report)."""
from __future__ import annotations

from typing import Any

BRIEF_TEMPLATE = """\
[Research experiment {node_key}]

You are an executor for an autonomous research run. Implement and
evaluate EXACTLY ONE hypothesis in YOUR OWN git worktree.

## Worktree (already created for you — work ONLY here)
- path: {worktree_path}
- branch: {branch}
- Never `git merge`, never touch {trunk_branch} or main/master, never
  leave your worktree. Commit your changes on your branch when done.

## Hypothesis
{hypothesis}

## Ancestor insights (lessons from the tree, root → parent)
{ancestor_insights}

## Evaluation (B_dev ONLY)
- command (run from your worktree): {eval_cmd}
- timeout: {eval_timeout}s. Long runs: use terminal(background=true,
  notify_on_complete=true) + process(wait); checkpoint to /workspace.
- The held-out test split is OFF LIMITS. Do not look for it, do not
  run it. Merging is the coordinator's job through a verified gate.

## Report contract (MANDATORY)
Finish by calling worker_complete with:
- summary: what you changed, what you observed, eval output tail
- metadata: {{"node_key": "{node_key}", "score": <float B_dev score>,
  "insight": "<one transferable lesson>", "result": "<1-line outcome>",
  "branch": "{branch}"}}
A timeout or failure is still a result — report it with score=null
and the failure as the insight.
{extra_context}"""


def build_executor_brief(
    *, node: Any, run: Any, worktree_path: str, branch: str,
    ancestor_insights: list[tuple[str, str]], extra_context: str = "",
) -> str:
    meta = run.meta or {}
    insights = "\n".join(
        f"- [{k}] {v}" for k, v in ancestor_insights if v
    ) or "(none yet)"
    return BRIEF_TEMPLATE.format(
        node_key=node.node_key,
        worktree_path=worktree_path,
        branch=branch,
        trunk_branch=run.trunk_branch,
        hypothesis=node.hypothesis,
        ancestor_insights=insights,
        eval_cmd=meta.get("eval_cmd") or "(ask the coordinator — not configured)",
        eval_timeout=meta.get("eval_timeout", 1800),
        extra_context=f"\n## Extra context\n{extra_context}" if extra_context else "",
    )


def build_report(run: Any, nodes: list[Any]) -> str:
    """Final REPORT.md — held-out test scores primary (the
    final-report-uses-TEST rule), top-10 dev-scored nodes, root insight."""
    meta = run.meta or {}
    direction = meta.get("metric_direction", "maximize")
    scored = sorted(
        (n for n in nodes if n.score is not None and n.node_key != "ROOT"),
        key=lambda n: n.score, reverse=(direction == "maximize"),
    )
    merged = [n for n in nodes if n.status == "merged"]
    root = next((n for n in nodes if n.node_key == "ROOT"), None)
    lines = [
        f"# Research Report — {meta.get('objective', '(objective)')}",
        "",
        "## Held-out test (authoritative)",
        f"- baseline: {meta.get('test_baseline_score')}",
        f"- final trunk: {meta.get('test_trunk_score')} ({direction})",
        "",
        "## Root insight",
        (root.insight if root and root.insight else "(none recorded)"),
        "",
        "## Merged ideas",
    ]
    lines += [f"- {n.node_key} dev={n.score}: {(n.hypothesis or '').splitlines()[0]}"
              for n in merged] or ["(none)"]
    lines += ["", "## Top ideas by dev score"]
    lines += [f"- {n.node_key} [{n.status}] dev={n.score}: "
              f"{(n.hypothesis or '').splitlines()[0][:120]}"
              for n in scored[:10]] or ["(none)"]
    lines += ["", "## Tree"]
    for n in sorted(nodes, key=lambda x: ([0] if x.node_key == "ROOT" else [1], x.node_key)):
        indent = "  " * (0 if n.node_key == "ROOT" else n.depth)
        lines.append(f"{indent}- {n.node_key} [{n.status}]")
    return "\n".join(lines)
```

- [ ] **Step 4: Implement the real `_DISPATCH_SCHEMA` + handler** in `surogates/tools/builtin/arbor.py`:

```python
_DISPATCH_SCHEMA = ToolSchema(
    name="dispatch_experiments",
    description=(
        "Dispatch 1-4 pending leaf hypotheses to executor workers, each in "
        "an isolated git worktree. Validates budget, depth, and parallelism "
        "before spawning. action='baseline' runs the baseline experiment."
    ),
    parameters={
        "type": "object",
        "properties": {
            "node_keys": {"type": "array", "items": {"type": "string"},
                          "minItems": 1, "maxItems": 4},
            "extra_context": {"type": "string"},
            "action": {"type": "string", "enum": ["experiments", "baseline"]},
        },
        "required": ["node_keys"],
    },
)


def _slug(text: str, n: int = 24) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n] or "exp"


async def _sandbox_sh(kwargs, command: str, timeout: int = 120) -> str:
    from surogates.sandbox.base import default_sandbox_spec
    pool = kwargs["sandbox_pool"]
    owner = str(kwargs["session_id"])
    await pool.ensure(owner, default_sandbox_spec())
    return await pool.execute(owner, "terminal", json.dumps({
        "command": command, "timeout": timeout,
    }))


async def _ancestor_insights(store, run_id, node) -> list[tuple[str, str]]:
    chain: list[tuple[str, str]] = []
    key = node.parent_key
    while key is not None:
        parent = await store.get_node(run_id, key)
        chain.append((parent.node_key, parent.insight or ""))
        key = parent.parent_key
    return list(reversed(chain))  # root → parent


async def _dispatch_experiments_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    import uuid as _uuid
    from surogates.tasks.service import create_task_and_spawn

    store, run_id = await _require_run(
        kwargs.get("session_config") or {}, kwargs["session_factory"],
    )
    run = await store.get_run(run_id)
    meta = run.meta or {}
    node_keys: list[str] = list(arguments.get("node_keys") or [])

    # ---- hard gates (budget enforcement does NOT ride the mission cap) ----
    spent = await store.cycles_spent(run_id)
    max_cycles = int(meta.get("max_cycles", 20))
    if spent >= max_cycles:
        return json.dumps({"error": (
            f"cycle budget spent ({spent}/{max_cycles}) — merge the best, "
            "prune the rest, and finalize"
        )})
    in_flight = await store.in_flight_count(run_id)
    max_parallel = int(meta.get("max_parallel", 2))
    if in_flight + len(node_keys) > max_parallel:
        return json.dumps({"error": (
            f"max_parallel={max_parallel} exceeded "
            f"({in_flight} in flight, {len(node_keys)} requested)"
        )})
    all_nodes = await store.list_nodes(run_id)
    children_of = {n.parent_key for n in all_nodes}
    for key in node_keys:
        node = await store.get_node(run_id, key)
        if node.status != "pending":
            return json.dumps({"error": f"node {key} is {node.status}, not pending"})
        if key in children_of:
            return json.dumps({"error": f"node {key} is not a leaf"})

    if not meta.get("eval_cmd"):
        return json.dumps({"error": "meta.eval_cmd is not set — run intake/set_meta first"})

    dispatched: list[str] = []
    for key in node_keys:
        node = await store.get_node(run_id, key)
        sha8 = _uuid.uuid4().hex[:8]
        branch = f"{run.branch_prefix}/n{key}-{_slug(node.hypothesis)}-{sha8}"
        worktree = f"/workspace/.arbor/worktrees/{key}"
        # Trunk is created lazily from the repo's HEAD on first dispatch —
        # nothing else creates it on a fresh run.
        out = await _sandbox_sh(kwargs, (
            f"cd {run.repo_path} && "
            f"(git rev-parse --verify {run.trunk_branch} >/dev/null 2>&1 "
            f"|| git branch {run.trunk_branch}) && "
            f"git worktree add -b {branch} {worktree} {run.trunk_branch} 2>&1"
        ))
        if "fatal" in (out or "").lower():
            return json.dumps({"error": f"worktree creation failed for {key}: {out[:500]}"})
        brief = __import__("surogates.arbor.prompts", fromlist=["build_executor_brief"]).build_executor_brief(
            node=node, run=run, worktree_path=worktree, branch=branch,
            ancestor_insights=await _ancestor_insights(store, run_id, node),
            extra_context=arguments.get("extra_context") or "",
        )
        await _persist_workspace_file(
            kwargs, path=f".arbor/experiments/{key}/executor_prompt.md",
            content=brief,
        )
        task_id = await create_task_and_spawn(
            goal=brief,
            agent_def_name="arbor-executor",
            max_attempts=1,  # a failed experiment is evidence, not a retryable crash
            mission_id=run.mission_id,
            parent_session_id=kwargs["session_id"],
            org_id=run.org_id,
            session_factory=kwargs["session_factory"],
            session_store=kwargs["session_store"],
            tenant=kwargs.get("tenant"),
            redis=kwargs.get("redis"),
        )
        from datetime import datetime, timezone
        await store.update_node(
            run_id, key, status="running", task_id=task_id,
            code_ref=branch, dispatched_at=datetime.now(timezone.utc),
        )
        dispatched.append(key)

    return json.dumps({"dispatched": dispatched,
                       "cycles_spent": spent, "max_cycles": max_cycles})
```

Replace the `__import__` contortion with a normal top-of-function import (`from surogates.arbor.prompts import build_executor_brief`) — it is written inline above only to keep the snippet single-block. `action="baseline"` for v1: when intake provided `baseline_score` it is unused; implement it as a normal dispatch whose brief says "measure the unmodified baseline" only if time permits — otherwise return a clear error directing intake to provide the baseline (do NOT leave a silent stub).

- [ ] **Step 5: Run the tests** — `pytest tests/integration/test_arbor_tools.py -x -q` → dispatch tests pass.

- [ ] **Step 6: Commit**

```bash
git add surogates/arbor/prompts.py surogates/tools/builtin/arbor.py tests/integration/test_arbor_tools.py
git commit -m "feat: dispatch_experiments with budget/leaf/depth gates and server-side worktrees"
```

---

### Task 7: `merge_experiment` — the bypass-proof gate (detached start/status)

**Files:**
- Modify: `surogates/tools/builtin/arbor.py`
- Test: extend `tests/integration/test_arbor_tools.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio(loop_scope="session")
async def test_merge_schema_accepts_no_score_argument():
    from surogates.tools.builtin.arbor import _MERGE_SCHEMA
    props = _MERGE_SCHEMA.parameters["properties"]
    assert "score" not in props and "test_score" not in props


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_requires_eval_cmd_test(merge_env):
    store, run_id, kwargs = merge_env  # node "1" is done, branch exists
    # eval_cmd_test deliberately unset
    out = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "1"}, **kwargs,
    ))
    assert "eval_cmd_test" in out["error"]  # hard error, no LLM fallback


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_blocks_on_no_improvement(merge_env_with_eval):
    store, run_id, kwargs, pool = merge_env_with_eval
    await store.set_meta(run_id, {"test_baseline_score": 0.50},
                         allow_machine_keys=True)
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    pool.responses['cat /workspace/.arbor/merge-eval/1/result.json'] = (
        '{"score": 0.40}'
    )
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is False and out["test_score"] == 0.40
    run = await store.get_run(run_id)
    assert "test_trunk_score" not in (run.meta or {})  # gate did NOT write


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_merges_on_improvement_and_writes_score(merge_env_with_eval):
    store, run_id, kwargs, pool = merge_env_with_eval
    await store.set_meta(run_id, {"test_baseline_score": 0.50},
                         allow_machine_keys=True)
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    pool.responses['cat /workspace/.arbor/merge-eval/1/result.json'] = (
        '{"score": 0.61}'
    )
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is True
    assert (await store.get_run(run_id)).meta["test_trunk_score"] == 0.61
    assert (await store.get_node(run_id, "1")).status == "merged"
    assert any("git merge --no-ff" in i for (_, _, i) in pool.calls)


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_reports_stale_eval(merge_env_with_eval):
    store, run_id, kwargs, pool = merge_env_with_eval
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    # rewind started_at past eval_timeout + grace
    run = await store.get_run(run_id)
    stamp = dict(run.meta["merge_eval"]); stamp["started_at"] = "2000-01-01T00:00:00+00:00"
    await store.set_meta(run_id, {"merge_eval": stamp}, allow_machine_keys=True)
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out.get("stale") is True  # orphaned by pod recycle → offer re-start
```

`merge_env` / `merge_env_with_eval` fixtures: same construction (and the same `@pytest_asyncio.fixture(loop_scope="session")` decorator) as `dispatch_env`, plus node "1" set `status="done", score=0.55, code_ref="research/run1/n1-x-abcd1234"`; `merge_env_with_eval` additionally sets `eval_cmd_test` and uses a `FakeSandboxPool` whose default response for `result.json` reads is `""` (file absent) until the test scripts it.

- [ ] **Step 2: Run to verify failure** — stub handler.

- [ ] **Step 3: Implement the real `_MERGE_SCHEMA` + handler** (port of `git_ops.py:109-561`, stricter — no score argument, no LLM fallback):

```python
_MERGE_SCHEMA = ToolSchema(
    name="merge_experiment",
    description=(
        "Merge a done experiment into trunk ONLY after this tool itself "
        "re-runs the held-out test eval in a detached worktree. "
        "start(node_key) launches the eval and returns immediately; "
        "status(node_key) reads the result and finalizes. There is no "
        "way to pass a score."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "status"]},
            "node_key": {"type": "string"},
        },
        "required": ["action", "node_key"],
    },
)


async def _merge_experiment_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    from datetime import datetime, timezone
    from surogates.arbor.store import is_improvement

    store, run_id = await _require_run(
        kwargs.get("session_config") or {}, kwargs["session_factory"],
    )
    run = await store.get_run(run_id)
    meta = run.meta or {}
    key = arguments["node_key"]
    node = await store.get_node(run_id, key)
    evald = f"/workspace/.arbor/merge-eval/{key}"

    if arguments["action"] == "start":
        if node.status != "done":
            return json.dumps({"error": f"node {key} is {node.status}, not done"})
        if not node.code_ref:
            return json.dumps({"error": f"node {key} has no branch recorded"})
        if not meta.get("eval_cmd_test"):
            return json.dumps({"error": (
                "meta.eval_cmd_test is not set — a research run without a "
                "held-out eval cannot merge (no LLM-reported fallback)"
            )})
        if run.trunk_branch in ("main", "master"):
            return json.dumps({"error": "refusing to operate on main/master as trunk"})
        timeout = int(meta.get("eval_timeout", 1800))
        await _sandbox_sh(kwargs, (
            f"rm -rf {evald} && mkdir -p {evald} && "
            f"cd {run.repo_path} && "
            f"git worktree add --detach {evald}/wt {node.code_ref} && "
            f"cd {evald}/wt && "
            f"nohup sh -c '{meta['eval_cmd_test']} > {evald}/eval.log 2>&1; "
            f"echo \"{{\\\"score\\\": $(tail -1 {evald}/eval.log)}}\" "
            f"> {evald}/result.json.tmp 2>/dev/null; "
            f"mv {evald}/result.json.tmp {evald}/result.json' "
            f">/dev/null 2>&1 &"
        ), timeout=60)
        await store.set_meta(run_id, {"merge_eval": {
            "node_key": key,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }}, allow_machine_keys=True)
        return json.dumps({"started": key,
                           "note": f"poll with merge_experiment(status, {key!r})"})

    # ---- status ----
    stamp = meta.get("merge_eval") or {}
    if stamp.get("node_key") != key:
        return json.dumps({"error": f"no merge eval started for {key}"})
    raw = await _sandbox_sh(kwargs, f"cat {evald}/result.json 2>/dev/null")
    if not (raw or "").strip():
        started = datetime.fromisoformat(stamp["started_at"])
        grace = int(meta.get("eval_timeout", 1800)) + 300
        age = (datetime.now(timezone.utc) - started).total_seconds()
        if age > grace:
            return json.dumps({"stale": True, "age_seconds": int(age),
                               "note": "eval orphaned (pod recycle?) — re-run start"})
        return json.dumps({"running": True, "age_seconds": int(age)})
    try:
        score = float(json.loads(raw)["score"])
    except Exception:
        return json.dumps({"error": (
            "eval produced no parsable {\"score\": ...} JSON — the eval "
            f"contract requires it; see {evald}/eval.log"
        )})

    reference = meta.get("test_trunk_score", meta.get("test_baseline_score"))
    direction = meta.get("metric_direction", "maximize")
    if not is_improvement(score, reference, direction):
        await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
        return json.dumps({
            "merged": False, "test_score": score, "reference": reference,
            "note": "held-out eval shows no improvement — treat as tree evidence",
        })

    threshold = float(meta.get("merge_threshold", 0.0))
    warn = None
    if reference is not None and abs(score - reference) < threshold:
        warn = (f"below merge_threshold={threshold} but improving — "
                "merging per Arbor's soft-threshold semantics")

    # Protected paths guard, then the merge itself, conflict-safe.
    protected = meta.get("protected_paths") or []
    if protected:
        diff = await _sandbox_sh(kwargs, (
            f"cd {run.repo_path} && "
            f"git diff --name-only {run.trunk_branch}...{node.code_ref}"
        ))
        hit = [p for p in protected
               for f in (diff or "").splitlines() if f.strip().startswith(p)]
        if hit:
            await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
            return json.dumps({"merged": False,
                               "error": f"branch touches protected paths: {hit}"})
    out = await _sandbox_sh(kwargs, (
        f"cd {run.repo_path} && git checkout {run.trunk_branch} && "
        f"git merge --no-ff {node.code_ref} "
        f"-m 'research: merge {key} (test={score})' 2>&1 "
        f"|| (git merge --abort; echo MERGE_CONFLICT)"
    ), timeout=120)
    if "MERGE_CONFLICT" in (out or ""):
        await store.set_meta(run_id, {"merge_eval": {}}, allow_machine_keys=True)
        return json.dumps({"merged": False,
                           "error": "merge conflict — trunk restored; rebase the branch"})

    await store.set_meta(run_id, {
        "test_trunk_score": score, "trunk_score": node.score, "merge_eval": {},
    }, allow_machine_keys=True)
    await store.update_node(run_id, key, status="merged")
    result = {"merged": True, "test_score": score}
    if warn:
        result["warning"] = warn
    return json.dumps(result)
```

The nohup result-writer above assumes the eval prints the score as its last log line — that is too fragile to keep. While implementing, replace it with the eval contract from the spec: `eval_cmd_test` itself must write `{"score": <float>}` to stdout's last line OR to `result.json`; the launcher becomes `nohup sh -c '{cmd} > {evald}/eval.log 2>&1; <python one-liner that extracts the last JSON object from eval.log into result.json>' &`. Write that python one-liner for real (10 lines, stdlib only) and unit-test it against three eval.log shapes (clean JSON line, noisy log + JSON line, no JSON → no result.json). The eval retry policy (`eval_retries` with backoff) wraps the start action: status reporting a parse failure includes `retries_left`, and `start` decrements it — implement as a counter inside the `merge_eval` stamp.

- [ ] **Step 4: Run the tests** — `pytest tests/integration/test_arbor_tools.py -x -q` → all merge tests pass.

- [ ] **Step 5: Commit**

```bash
git add surogates/tools/builtin/arbor.py tests/integration/test_arbor_tools.py
git commit -m "feat: merge_experiment gate with detached held-out eval and no score argument"
```

---

### Task 8: Harvest mixin + `research.*` events

**Files:**
- Create: `surogates/harness/loop_arbor.py`
- Modify: `surogates/session/events.py` (after the `MISSION_*` block, ~line 150), `surogates/harness/loop.py` (call beside `maybe_emit_board_update`, ~line 1304)
- Test: `tests/test_arbor_harvest.py` (new)

- [ ] **Step 1: Add the event types** in `surogates/session/events.py` next to the mission block:

```python
    RESEARCH_DEFINED = "research.defined"
    RESEARCH_DISPATCHED = "research.dispatched"
    RESEARCH_HARVESTED = "research.harvested"
    RESEARCH_MERGED = "research.merged"
    RESEARCH_PRUNED = "research.pruned"
    RESEARCH_CONVERGED = "research.converged"
    RESEARCH_REPORT = "research.report"
```

- [ ] **Step 2: Write the failing harvest test**

```python
# tests/test_arbor_harvest.py
"""Unit tests for the deterministic harvest fold (stubbed task rows)."""
import pytest

from surogates.harness.loop_arbor import fold_task_into_node


class _Node:
    node_key = "1"; status = "running"; insight = None


class _Task:
    status = "completed"
    result = "long prose report"
    result_metadata = {"node_key": "1", "score": 0.42,
                       "insight": "lesson", "result": "ok", "branch": "b"}


class _FailedTask:
    status = "failed"; result = None; result_metadata = None


class _StubStore:
    def __init__(self):
        self.updates = []
    async def update_node(self, run_id, key, **fields):
        self.updates.append((key, fields))
    async def get_node(self, run_id, key):
        return _Node()
    async def list_nodes(self, run_id):
        return []


@pytest.mark.asyncio
async def test_fold_uses_metadata_verbatim():
    store = _StubStore()
    out = await fold_task_into_node(store, "run", _Task(), llm_client=None)
    key, fields = store.updates[0]
    assert key == "1" and fields["status"] == "done"
    assert fields["score"] == 0.42 and fields["insight"] == "lesson"
    assert out["folded"] == "1"


@pytest.mark.asyncio
async def test_fold_failed_task_spends_budget_as_failed():
    store = _StubStore()
    t = _FailedTask(); t.result_metadata = {"node_key": "1"}
    await fold_task_into_node(store, "run", t, llm_client=None)
    _, fields = store.updates[0]
    assert fields["status"] == "failed"
    assert "crash" in fields["insight"].lower() or "fail" in fields["insight"].lower()
```

- [ ] **Step 3: Run to verify failure**, then **implement `surogates/harness/loop_arbor.py`** (mirror `BoardMixin`'s never-raise discipline):

```python
"""Pre-LLM harvest hook for research missions.

Runs at coordinator wake, BEFORE the LLM call, fail-open: folds every
running idea_node whose Task is terminal into the tree, concat-propagates
the insight, removes the worktree (branch kept), and appends a
[research harvest] digest + fresh constraints block at end-of-history
(BoardMixin idiom — append only, never insert mid-list).
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_TERMINAL_TASK = ("completed", "failed", "cancelled")


async def fold_task_into_node(
    store, run_id, task, *, llm_client, model: str | None = None,
) -> dict:
    """Deterministic, metadata-first fold of one terminal Task."""
    md = dict(task.result_metadata or {})
    key = md.get("node_key")
    if not key:
        return {"skipped": "no node_key in result_metadata"}
    if task.status == "completed" and md:
        fields = {
            "status": "done",
            "score": md.get("score"),
            "insight": str(md.get("insight") or "")[:2000],
            "result": str(md.get("result") or "")[:500],
        }
    elif task.status == "completed" and task.result and llm_client is not None and model:
        from surogates.arbor.models import ExperimentReport
        from surogates.harness.structured_output import generate_structured
        text = task.result
        if len(text) > 12000:
            text = text[:6000] + "\n[... middle truncated ...]\n" + text[-6000:]
        try:
            rep = await generate_structured(
                llm_client=llm_client, model=model,
                messages=[{"role": "user", "content":
                           f"Extract the experiment report fields:\n\n{text}"}],
                output_model=ExperimentReport, max_tokens=600, temperature=0,
            )
            fields = {"status": "done", "score": rep.score,
                      "insight": rep.insight[:2000], "result": rep.result[:500]}
        except Exception:
            fields = {"status": "done", "score": None,
                      "insight": "(report extraction failed)",
                      "result": (task.result or "")[:500]}
    else:
        fields = {"status": "failed", "score": None,
                  "insight": f"Timed out/crashed: task ended {task.status}",
                  "result": (task.result or "")[:500]}
    await store.update_node(run_id, key, **fields)

    # deterministic concat-propagate up the chain
    from surogates.arbor.propagate import concat_propagate
    nodes = await store.list_nodes(run_id)
    insights = {n.node_key: n.insight for n in nodes}
    parents = {n.node_key: n.parent_key for n in nodes if n.parent_key}
    for ancestor, merged in concat_propagate(
        node_key=key, insight=fields.get("insight") or "",
        insights=insights, parents=parents,
    ).items():
        await store.update_node(run_id, ancestor, insight=merged)
    return {"folded": key, **{k: v for k, v in fields.items() if k != "result"}}


class ArborHarvestMixin:
    async def maybe_harvest_research(self, session: Any, messages: list[dict]) -> None:
        """Top-of-iteration research hook. Never raises."""
        try:
            await self._harvest_inner(session, messages)
        except Exception:
            logger.exception(
                "research: harvest hook failed for session %s (continuing)",
                session.id,
            )

    async def _harvest_inner(self, session: Any, messages: list[dict]) -> None:
        config = session.config or {}
        raw = config.get("active_research_run_id")
        if not raw:
            return
        from sqlalchemy import select
        from surogates.arbor.store import ResearchStore
        from surogates.db.models import IdeaNode, Task
        from surogates.session.events import EventType

        run_id = UUID(str(raw))
        store = ResearchStore(self._session_factory)
        async with self._session_factory() as db:
            rows = (await db.execute(
                select(IdeaNode, Task)
                .join(Task, IdeaNode.task_id == Task.id)
                .where(IdeaNode.run_id == run_id,
                       IdeaNode.status == "running",
                       Task.status.in_(_TERMINAL_TASK))
            )).all()
        if not rows:
            return
        digests = []
        for node, task in rows:
            folded = await fold_task_into_node(
                store, run_id, task,
                llm_client=getattr(self, "_llm_client", None),
                model=getattr(self, "_eval_model", None),
            )
            digests.append(folded)
            # worktree cleanup — branch survives (Arbor's invariant)
            run = await store.get_run(run_id)
            await self._research_sandbox_sh(session, (
                f"cd {run.repo_path} && "
                f"git worktree remove --force "
                f"/workspace/.arbor/worktrees/{node.node_key} 2>/dev/null; true"
            ))
        constraints = await store.constraints_block(run_id)
        content = (
            "[research harvest]\n" + json.dumps(digests, default=str)
            + "\n\n" + constraints
        )
        await self._store.emit_event(
            session.id, EventType.RESEARCH_HARVESTED,
            {"run_id": str(run_id), "folded": [d.get("folded") for d in digests]},
        )
        messages.append({"role": "user", "content": content})
```

`_research_sandbox_sh` mirrors `_sandbox_sh` from the tools module against `self._sandbox_pool` — read how `AgentHarness` holds the pool/store attributes (`self._store`, `self._session_factory` confirmed by `loop_board.py:67,90`) and adapt names to the real ones. The `model` kwarg must be the same eval-model name the mission judge is built with (`_build_mission_judge`'s `eval_model` — find where the harness wires it and reuse that attribute); when unset, fold skips LLM extraction and fails open to the truncated-text path.

- [ ] **Step 4: Wire the hook** in `surogates/harness/loop.py` next to the board call (~line 1304):

```python
            await self.maybe_harvest_research(session, messages)
```

and add `ArborHarvestMixin` to `AgentHarness`'s base classes where `BoardMixin` is listed.

- [ ] **Step 5: Run** `pytest tests/test_arbor_harvest.py -x -q` → pass. Run `pytest tests/harness/ -q` → no regressions.

- [ ] **Step 6: Commit**

```bash
git add surogates/harness/ surogates/session/events.py tests/test_arbor_harvest.py
git commit -m "feat: deterministic research harvest at coordinator wake with research events"
```

---

### Task 9: `/auto-research` command

**Files:**
- Modify: `surogates/missions/commands.py`, `surogates/harness/slash_skill.py:32-40`, `surogates/harness/loop.py` (~line 924)
- Test: `tests/test_arbor_parse.py` (new)

- [ ] **Step 1: Write the failing parse tests**

```python
# tests/test_arbor_parse.py
import pytest

from surogates.missions.commands import (
    MissionCommandParseError, parse_auto_research_command,
)


def test_parse_create_with_leading_kv_tokens():
    cmd = parse_auto_research_command(
        "repo=/workspace/repo max_iterations=60 baseline=0.41 baseline_test=0.50 "
        "Improve F1\n\nRubric:\n- test_trunk_score improves"
    )
    assert cmd.action == "create"
    assert cmd.max_iterations == 60
    assert cmd.repo == "/workspace/repo"
    assert cmd.baseline == 0.41 and cmd.baseline_test == 0.50
    assert cmd.description.startswith("Improve F1")
    assert "test_trunk_score" in cmd.rubric


def test_parse_rejects_non_numeric_baseline():
    with pytest.raises(MissionCommandParseError):
        parse_auto_research_command("baseline=abc x\n\nRubric:\nr")


def test_parse_resume_token():
    cmd = parse_auto_research_command("resume=2b1d34aa max_iterations=40 continue\n\nRubric:\nr")
    assert cmd.resume_run == "2b1d34aa" and cmd.max_iterations == 40


def test_parse_control_verbs_delegate():
    assert parse_auto_research_command("pause taking a break").action == "pause"
    assert parse_auto_research_command("").action == "status"


def test_parse_requires_rubric():
    with pytest.raises(MissionCommandParseError):
        parse_auto_research_command("max_iterations=10 no rubric here")
```

- [ ] **Step 2: Run to verify failure**, then **implement in `surogates/missions/commands.py`**:

```python
@dataclass(slots=True)
class AutoResearchCommand(MissionCommand):
    """Parsed /auto-research invocation — a MissionCommand plus
    research-specific leading key=value tokens."""

    max_iterations: int | None = None
    resume_run: str | None = None
    repo: str | None = None
    baseline: float | None = None
    baseline_test: float | None = None


_KV_RE = re.compile(
    r"^(max_iterations|resume|repo|baseline|baseline_test)=(\S+)\s*"
)


def parse_auto_research_command(raw: str) -> AutoResearchCommand:
    """Alias of /mission: same control verbs and Rubric: contract, plus
    optional leading ``repo=`` / ``max_iterations=`` / ``baseline=`` /
    ``baseline_test=`` / ``resume=`` tokens."""
    text = (raw or "").strip()
    kv: dict[str, str] = {}
    while True:
        m = _KV_RE.match(text)
        if not m:
            break
        kv[m.group(1)] = m.group(2)
        text = text[m.end():]

    def _as_int(key: str) -> int | None:
        if key not in kv:
            return None
        try:
            return int(kv[key])
        except ValueError:
            raise MissionCommandParseError(
                f"{key} must be an integer, got {kv[key]!r}"
            )

    def _as_float(key: str) -> float | None:
        if key not in kv:
            return None
        try:
            return float(kv[key])
        except ValueError:
            raise MissionCommandParseError(
                f"{key} must be a number, got {kv[key]!r}"
            )

    base = parse_mission_command(text)
    return AutoResearchCommand(
        action=base.action, description=base.description, rubric=base.rubric,
        reason=base.reason, cascade_to_workers=base.cascade_to_workers,
        max_iterations=_as_int("max_iterations"),
        resume_run=kv.get("resume"),
        repo=kv.get("repo"),
        baseline=_as_float("baseline"),
        baseline_test=_as_float("baseline_test"),
    )
```

- [ ] **Step 3: Implement `handle_research_mission_create`** in the same file — wraps the existing create with research stamping (the `resume=` path is v3; for v1 return a clear "resume lands in a later release" error when set):

```python
_RESEARCH_KICKOFF_TEMPLATE = """\
[Research mission kickoff]

Objective: {description}

Rubric:
{rubric}

You are this research run's coordinator (Arbor protocol). Your loop:
idea_tree(view, format=constraints) → OBSERVE → IDEATE (load the
arbor-ideate skill — hard gate) → idea_tree(add) 1-3 four-line
hypotheses → dispatch_experiments(node_keys=[...]) → end your turn.
Harvest is automatic at your next wake. DECIDE with
merge_experiment(start/status) and idea_tree(prune). You cannot edit
code or run commands — executors do; you steer the tree.
First action now: idea_tree(set_meta) with the contract values from
the message above, then ideate.
"""


async def handle_research_mission_create(
    *,
    cmd: AutoResearchCommand,
    session_id: UUID,
    org_id: UUID,
    agent_id: str,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
) -> MissionHandlerResult:
    """Create a research-kind mission: Mission row + research_runs row +
    ROOT node + research config stamping. /mission's create is untouched."""
    if cmd.resume_run:
        return MissionHandlerResult(
            ok=False, error="resume=<run> is not supported yet",
        )
    if not cmd.repo or not cmd.repo.startswith("/workspace/"):
        return MissionHandlerResult(
            ok=False,
            error="repo=</workspace/...> is required for /auto-research create",
        )
    base = await handle_mission_create(
        description=cmd.description, rubric=cmd.rubric,
        session_id=session_id, org_id=org_id, agent_id=agent_id,
        session_store=session_store, session_factory=session_factory,
        mission_store=mission_store,
        user_id=user_id, service_account_id=service_account_id,
        max_iterations=cmd.max_iterations or 20,
    )
    if not base.ok:
        return base

    from surogates.arbor.store import ResearchStore
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=base.mission_id, session_id=session_id,
        agent_id=agent_id, repo_path=cmd.repo,
        trunk_branch=f"research/run-{str(base.mission_id)[:8]}/trunk",
        branch_prefix=f"research/run-{str(base.mission_id)[:8]}",
        objective=cmd.description,
    )
    baseline_meta: dict[str, Any] = {}
    if cmd.baseline is not None:
        baseline_meta["baseline_score"] = cmd.baseline
    if cmd.baseline_test is not None:
        baseline_meta["test_baseline_score"] = cmd.baseline_test
    if baseline_meta:
        # Server-side write — test_baseline_score is a machine key the
        # coordinator's set_meta rejects; intake measured these numbers
        # and the merge gate needs them as its reference.
        await store.set_meta(run_id, baseline_meta, allow_machine_keys=True)

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        cfg = dict(sess.config or {})
        cfg["active_research_run_id"] = str(run_id)
        preloaded = [s for s in (cfg.get("preloaded_skills") or [])
                     if s != "subagent-task-orchestrator"]
        if "arbor-coordinator" not in preloaded:
            preloaded.append("arbor-coordinator")
        cfg["preloaded_skills"] = preloaded
        sess.config = cfg
        await db.commit()

    await session_store.emit_event(
        session_id, EventType.RESEARCH_DEFINED,
        {"mission_id": str(base.mission_id), "run_id": str(run_id)},
    )
    base.kickoff_content = _RESEARCH_KICKOFF_TEMPLATE.format(
        description=cmd.description, rubric=cmd.rubric,
    )
    return base
```

`handle_mission_create` changes needed by the call above: add a `max_iterations: int = 20` parameter, pass it to `store.create` AND the `mission.defined` event payload (today both hardcode 20 — `commands.py:224,261`); `/mission`'s caller keeps the default. The research path also removes the `subagent-task-orchestrator` preload that `handle_mission_create` stamps (shown above) — keep that removal; the research coordinator runs the arbor protocol instead.

- [ ] **Step 4: Wire the slash command.** In `surogates/harness/slash_skill.py:32-40` add `"auto-research",` to `_BUILTIN_SLASH_COMMANDS`. In `surogates/harness/loop.py` after the `/mission` match (~line 926):

```python
            if last_user_content == "/auto-research" or last_user_content.startswith("/auto-research "):
                await self._handle_auto_research_command(session, last_user_content, lease)
                return
```

`_handle_auto_research_command` mirrors `_handle_mission_command` (find it in loop.py; copy its structure): parse with `parse_auto_research_command`, route control verbs to the existing mission handlers, route create to `handle_research_mission_create`, and reuse the kickoff-after-cursor-advance emission contract VERBATIM (the cursor-race comment in `MissionHandlerResult` explains why; do not reorder).

- [ ] **Step 5: Run** `pytest tests/test_arbor_parse.py tests/missions/ -q` → new tests pass, missions tests unbroken.

- [ ] **Step 6: Commit**

```bash
git add surogates/missions/commands.py surogates/harness/ tests/test_arbor_parse.py
git commit -m "feat: add /auto-research command creating research-kind missions"
```

---

### Task 10: `research_coordinator` read-only carve-out

**Files:**
- Modify: `surogates/harness/loop.py::_tool_filter_for_session` (~line 3029)
- Test: `tests/harness/` — add `tests/test_arbor_tool_filter.py`

- [ ] **Step 1: Failing test** (instantiate or monkeypatch the harness the way existing `_tool_filter_for_session` tests do — search `tests/` for `strict_coordinator` and mirror that test's setup):

```python
# tests/test_arbor_tool_filter.py
"""Read-only OBSERVE carve-out: research coordinators get read tools back."""
READ_TOOLS = {"read_file", "search_files", "list_files"}
STILL_STRIPPED = {"terminal", "write_file", "patch", "web_search", "create_artifact"}


def test_research_coordinator_restores_reads_only(make_harness, make_session):
    h = make_harness(tool_names=READ_TOOLS | STILL_STRIPPED | {"idea_tree"})
    s = make_session(config={
        "coordinator": True, "strict_coordinator": True,
        "active_research_run_id": "r1",
    })
    allowed = h._tool_filter_for_session(s)
    assert READ_TOOLS <= allowed
    assert not (STILL_STRIPPED & allowed)


def test_plain_strict_coordinator_unchanged(make_harness, make_session):
    h = make_harness(tool_names=READ_TOOLS | STILL_STRIPPED)
    s = make_session(config={"coordinator": True, "strict_coordinator": True})
    allowed = h._tool_filter_for_session(s)
    assert not (READ_TOOLS & allowed)
```

(`make_harness` / `make_session` are illustrative — reuse the real fixtures/constructor the existing filter tests use; if none exist, construct the harness object the way `tests/harness/` does elsewhere.)

- [ ] **Step 2: Implement** inside the `strict_coordinator` branch (loop.py:3029-3035), after `excluded.update(COORDINATOR_IMPLEMENTATION_TOOLS)`:

```python
                # Research coordinators get READ access back for OBSERVE
                # forensics (failure logs, eval output). Writes, terminal,
                # web, and browser stay stripped — the strict-mode incident
                # class was the model doing the work, which still requires
                # tools it does not have.
                if config.get("active_research_run_id"):
                    user_excluded = set(config.get("excluded_tools") or [])
                    excluded -= (
                        {"read_file", "search_files", "list_files"}
                        - user_excluded
                    )
```

- [ ] **Step 3: Run** `pytest tests/test_arbor_tool_filter.py -x -q` → pass. **Commit:**

```bash
git add surogates/harness/loop.py tests/test_arbor_tool_filter.py
git commit -m "feat: restore read-only tools for research coordinators during OBSERVE"
```

---

### Task 11: Research evaluator policy

**Files:**
- Create: `surogates/arbor/evaluator_policy.py`
- Modify: `surogates/harness/loop_mission_evaluator.py::_maybe_run_mission_evaluator` (after the active-mission fetch, ~line 53)
- Test: `tests/test_arbor_evaluator_policy.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_arbor_evaluator_policy.py
import pytest

from surogates.arbor.evaluator_policy import (
    adjust_research_verdict, research_should_skip,
)


@pytest.mark.asyncio
async def test_skip_while_experiments_in_flight():
    class _Store:
        async def in_flight_count(self, run_id): return 2
    assert await research_should_skip(_Store(), "run") is True


def test_satisfied_requires_machine_written_score():
    meta = {"test_baseline_score": 0.5, "metric_direction": "maximize"}
    v = {"result": "satisfied", "explanation": "looks done", "feedback": ""}
    out = adjust_research_verdict(v, meta=meta, report_task_done=True)
    assert out["result"] == "needs_revision"          # no test_trunk_score
    meta["test_trunk_score"] = 0.6
    out = adjust_research_verdict(v, meta=meta, report_task_done=False)
    assert out["result"] == "needs_revision"          # report not done
    out = adjust_research_verdict(v, meta=meta, report_task_done=True)
    assert out["result"] == "satisfied"               # verified


def test_failed_and_blocked_demote_without_corroboration():
    meta = {}
    for r in ("failed", "blocked"):
        out = adjust_research_verdict(
            {"result": r, "explanation": "", "feedback": ""},
            meta=meta, report_task_done=False,
        )
        assert out["result"] == "needs_revision"
    out = adjust_research_verdict(
        {"result": "failed", "explanation": "", "feedback": ""},
        meta=meta, report_task_done=False, budget_exhausted=True,
    )
    assert out["result"] == "failed"                  # corroborated
```

- [ ] **Step 2: Implement `surogates/arbor/evaluator_policy.py`**

```python
"""Research-kind mission judge policy.

Wraps the standard LLM rubric judge in deterministic gates: never
evaluate while experiments are in flight; never honor `satisfied`
without machine-written held-out scores; demote terminal judge
verdicts that lack deterministic corroboration (a single noisy verdict
must not kill a 40-cycle run).
"""
from __future__ import annotations

from typing import Any

from surogates.arbor.store import is_improvement


async def research_should_skip(store: Any, run_id: Any) -> bool:
    return (await store.in_flight_count(run_id)) > 0


def adjust_research_verdict(
    verdict: dict[str, Any], *, meta: dict[str, Any],
    report_task_done: bool, budget_exhausted: bool = False,
) -> dict[str, Any]:
    result = verdict.get("result")
    if result == "satisfied":
        score = meta.get("test_trunk_score")
        improved = is_improvement(
            score, meta.get("test_baseline_score"),
            meta.get("metric_direction", "maximize"),
        )
        no_improvement_close = budget_exhausted  # explicit root-insight path
        if not ((improved or no_improvement_close) and report_task_done):
            return {
                "result": "needs_revision",
                "explanation": "satisfied rejected by deterministic verification",
                "feedback": (
                    "satisfied requires a machine-written test_trunk_score "
                    "improving on test_baseline_score AND the final report "
                    "task done. Merge through merge_experiment and finalize "
                    "with idea_tree(report) + a report task."
                ),
            }
        return verdict
    if result in ("failed", "blocked") and not budget_exhausted:
        return {
            "result": "needs_revision",
            "explanation": f"judge said {result} without deterministic corroboration",
            "feedback": verdict.get("feedback") or verdict.get("explanation") or "",
        }
    return verdict


def research_prompt_block(
    *, constraints_block: str, cycles_spent: int, max_cycles: int,
) -> str:
    """Tree leaderboard block injected into the judge's user prompt
    (replaces the 20-recent-tasks framing for research missions)."""
    return (
        "## Research run state (machine-written; the ONLY trusted scores)\n"
        f"cycles: {cycles_spent}/{max_cycles}\n\n"
        f"{constraints_block}\n\n"
        "Verdict guidance: needs_revision feedback must name the next "
        "structural step (expand X / prune Y / paradigm shift / merge / "
        "finalize). Never accept prose claims of improvement; only "
        "meta.test_trunk_score counts."
    )
```

Note: gating the no-improvement close on `budget_exhausted` alone is a deliberate v1 simplification of the spec's "explicit no-improvement root insight" condition; v2 tightens it by also requiring a non-empty ROOT insight.

- [ ] **Step 3: Wire the dispatch** in `_maybe_run_mission_evaluator` right after `active` is fetched and checked (loop_mission_evaluator.py:53-55):

```python
    # Research-kind missions: table-lookup dispatch. Standard missions
    # take the existing path untouched.
    from surogates.arbor.store import ResearchStore
    research_store = ResearchStore(session_factory)
    run = await research_store.get_run_for_mission(active.id)
    if run is not None:
        from surogates.arbor.evaluator_policy import research_should_skip
        if await research_should_skip(research_store, run.id):
            return  # no verdict, no iteration burn while experiments fly
```

Then, where `build_evaluator_prompt` is called, append `research_prompt_block(...)` to the user prompt when `run is not None`; and where `apply_verdict` is called, pass the verdict through `adjust_research_verdict(verdict, meta=run.meta or {}, report_task_done=<query: any completed task for this mission whose result_metadata has report=true>, budget_exhausted=<cycles_spent >= max_cycles>)` first. Each of those three integration points is ≤6 lines; keep the standard-mission path byte-identical when `run is None`.

- [ ] **Step 4: Run** `pytest tests/test_arbor_evaluator_policy.py tests/missions/ tests/harness/ -q` → green.

- [ ] **Step 5: Commit**

```bash
git add surogates/arbor/evaluator_policy.py surogates/harness/loop_mission_evaluator.py tests/test_arbor_evaluator_policy.py
git commit -m "feat: research mission evaluator policy with deterministic verdict gates"
```

---

### Task 12: The three v1 skills

**Files:**
- Create: `skills/research/arbor-research/SKILL.md`, `skills/research/arbor-coordinator/SKILL.md`, `skills/research/arbor-executor/SKILL.md`

Skills are prose — no TDD loop; validate frontmatter shape against an existing bundle (`skills/process/brainstorming/SKILL.md`) and keep each under ~120 lines. Port content from `study/Arbor/skills/` (the authors' own suite) backed by `src/coordinator/prompts.py:313-450` and `src/executor/prompts.py:239-401`. v1 cut only — ideate hard-gate depth and merge doctrine expand in v2.

- [ ] **Step 1: `arbor-research/SKILL.md`** (intake entry; the user types `/arbor-research <goal>`):

Frontmatter `name: arbor-research`, `description: Intake for autonomous research runs — discovers the repo/eval/splits, measures the baseline, confirms a Research Contract, then emits a ready-to-send /auto-research command.` Body sections (write them out fully when implementing, from the spec §4.6 step 1): DISCOVER (repo, eval script, dev/test splits, git state — must be clean), BASELINE (run eval on B_dev; run B_test ONCE; record both numbers), CLARIFY (one compact checkpoint: metric+direction, ambition, permissions, budget, HITL mode, smoke?), EMIT (a fenced `/auto-research repo=<path> max_iterations=<2×max_cycles> baseline=<measured dev score> baseline_test=<measured test score> <objective>\n\nRubric:\n<the machine-anchored rubric template from the spec §4.6>` block the user sends with one click; the baselines are written server-side at create, and the remaining contract values — eval_cmd, eval_cmd_test, metric_direction, protected_paths — are quoted in the contract for the coordinator's first `idea_tree(set_meta)`). Rule: NEVER start the mission yourself; the user sends the command.

- [ ] **Step 2: `arbor-coordinator/SKILL.md`** (preloaded by `/auto-research` create):

Frontmatter `name: arbor-coordinator`. Body: the cycle protocol — every turn starts `idea_tree(view, format=constraints)`; OBSERVE failure evidence with read tools; IDEATE 1-3 four-line hypotheses (`Mechanism: / Hypothesis: / Observable: / Conflicts:`) as CHILDREN of the most informative node; `dispatch_experiments` then END YOUR TURN (harvest is automatic); DECIDE: `merge_experiment(start)` for a `done` node that beats trunk on B_dev, `idea_tree(prune)` for dead branches with the lesson as reason; B_dev/B_test law (B_test only ever through merge_experiment; selecting on B_test = mission blocked); budget discipline (failed runs spend budget; don't requeue experiment failures — requeue is for infra deaths only); FINALIZE on budget/target: merge best → `idea_tree(report)` → spawn one report task that creates the artifact from `/workspace/.arbor/REPORT.md` and completes with `metadata={"report": true}`.

- [ ] **Step 3: `arbor-executor/SKILL.md`** (preloaded on workers via `AgentDef.preloaded_skills`):

Frontmatter `name: arbor-executor`. Body: the 7-step workflow (UNDERSTAND the worktree and hypothesis → BASELINE sanity-check the eval runs → PLAN minimal change → IMPLEMENT only inside your worktree → VALIDATE on 2-3 examples → full B_dev eval → REPORT via worker_complete with the exact metadata contract); long-run policy (`terminal(background=true, notify_on_complete=true)` + `process(wait)`, checkpoint to /workspace, experiments ≤45min in v1); prohibitions (never `git merge`, never main/master, never leave the worktree, never touch the test split, never install packages without the brief saying so); timeout-is-evidence (report failures honestly with score=null).

- [ ] **Step 4: Also create the ops-side `arbor-executor` AgentDef** at the location the deep-research feature pack uses (`surogate_ops/features/deep_research/agents/*/AGENT.md` is the precedent — create `surogate_ops/features/research_tree/agents/arbor-executor/AGENT.md` in the surogate-ops repo with frontmatter `name: arbor-executor`, `max_iterations: 80`, `preloaded_skills: [arbor-executor]` and a 10-line body pointing at the skill). NOTE: this file lives in `/work/surogate-ops`, not this repo — if executing this plan repo-by-repo, record it as a follow-up commit there. For local testing without ops, an `AgentDef` can also load from the user/org agents directory — check `surogates/tools/loader.py`'s search paths and drop the same AGENT.md there for the smoke test.

- [ ] **Step 5: Commit**

```bash
git add skills/research/
git commit -m "feat: add arbor research skill bundle (intake, coordinator, executor)"
```

---

### Task 13: Smoke-mode end-to-end test

**Files:**
- Test: `tests/integration/test_arbor_smoke.py` (new)

The CI-safe protocol regression: mocked scores, fake sandbox, fake spawner — no git, no training, no LLM. It exercises: create research mission → set_meta → add → dispatch (gates) → simulate worker completion → harvest fold → merge gate (scripted eval result) → evaluator policy verdict adjustment.

- [ ] **Step 1: Write the test** — compose the pieces already unit-tested: build the run via `handle_research_mission_create` (stub `session_store.emit_event`), assert session config got `active_research_run_id` + `arbor-coordinator` preload and the kickoff text contains "idea_tree(set_meta)"; then drive one full cycle through the store/tools/fold functions as in Tasks 6-8; finish by asserting `adjust_research_verdict` flips a prose `satisfied` to `needs_revision` before the merge and honors it after `test_trunk_score` is written by the merge handler. ~120 lines; every assertion already has a verified shape from the earlier tasks.

- [ ] **Step 2: Run the full suite**

Run: `pytest tests/ -q -x --ignore=tests/integration && pytest tests/integration -q -k arbor`
Expected: green. Then the repo's standard full run: `pytest tests/ -q` (budget ~10-15 min with testcontainers).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_arbor_smoke.py
git commit -m "test: add arbor research smoke-mode protocol regression"
```

---

## Self-review checklist (run after implementation)

1. **Spec coverage (v1 list, spec §6):** tables+store (T1-3) ✓ three tools incl. detached merge gate + staleness (T4,6,7) ✓ factoring + preloaded_skills (T5) ✓ `/auto-research` (T9) ✓ filter branch (T10) ✓ harvest (T8) ✓ evaluator policy (T11) ✓ events (T8) ✓ skills (T12) ✓ routing+gate tests (T4,6,7) ✓ smoke run (T13) ✓.
2. **Survival test from the spec's v1 exit criteria** — worker-process restart and pod recycle mid-run — is NOT automated here; verify manually on the toy repo before calling v1 done (kill the worker between dispatch and harvest; recycle the sandbox pod during a merge eval and confirm the staleness path).
3. **Every "adapt to the real signature" note** (T4 step 1/6, T6 kwargs, T8 attrs, T9 handler, T10 fixtures) is a read-then-adapt instruction, not optional: the named file MUST be read before writing that code.
4. **No `uv run`, no task numbers in commits, no co-author trailers.**

## Execution

Plan complete and saved. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task with review between tasks (use superpowers:subagent-driven-development).
2. **Inline Execution** — execute task-by-task in one session with checkpoints (use superpowers:executing-plans).
