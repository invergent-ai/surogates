"""Dispatch and merge gate tests for the arbor tools, with a fake sandbox.

No real git / K8s / LLM: a ``FakeSandboxPool`` records the shell commands
the handlers issue and returns scripted stdout, and ``create_task_and_spawn``
is patched so dispatch never touches the task layer.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from surogates.arbor.store import ResearchStore
from surogates.tools.builtin.arbor import (
    _dispatch_experiments_handler,
    _idea_tree_handler,
    _merge_experiment_handler,
)


class FakeSandboxPool:
    """Records exec calls; returns scripted stdout by command substring."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.calls: list[tuple[str, str, str]] = []
        self.responses: dict[str, str] = responses or {}

    async def ensure(self, session_id, spec):  # noqa: D401 - test stub
        return session_id

    async def execute(self, session_id, name, input):
        self.calls.append((session_id, name, input))
        for needle, out in self.responses.items():
            if needle in input:
                return out
        # The dispatch handoff bundles the repo and base64-encodes it;
        # return a valid one-line base64 so _bundle_branch_b64 succeeds.
        if "git bundle create" in input and "base64" in input:
            return "ZmFrZS1idW5kbGU="
        return ""


class _StubSessionStore:
    async def emit_event(self, *args, **kwargs):
        return None


def _fake_spawn(session_factory, org_id, parent_session_id):
    """Side effect for create_task_and_spawn: insert a REAL Task row (so the
    idea_nodes.task_id FK holds) and return the standard result dict."""
    async def _spawn(**kwargs):
        from surogates.db.models import Task

        async with session_factory() as db:
            task = Task(
                org_id=org_id, parent_session_id=parent_session_id,
                agent_def_name=kwargs.get("agent_def_name"),
                goal=kwargs.get("goal") or "g", status="running",
                max_attempts=kwargs.get("max_attempts", 1),
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)
            task_id = task.id
        return {"task_id": str(task_id), "status": "running",
                "worker_id": str(uuid.uuid4())}
    return _spawn


@pytest_asyncio.fixture(loop_scope="session")
async def dispatch_env(session_factory, seeded_org_and_session):
    """A run with eval_cmd + eval_cmd_test set, two pending leaves '1'/'2',
    max_cycles=2, max_parallel=1; plus the HARNESS handler kwargs."""
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
    pool = FakeSandboxPool()
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id),
        "sandbox_pool": pool,
        "session_store": _StubSessionStore(),
        "tenant": object(),
        "redis": object(),
    }
    return store, run_id, pool, kwargs


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_refuses_when_budget_spent(dispatch_env):
    store, run_id, _pool, kwargs = dispatch_env
    await store.update_node(run_id, "1", status="done", insight="i")
    await store.update_node(run_id, "2", status="failed", insight="t")
    # cycles_spent == max_cycles == 2 -> refuse before the per-node checks.
    out = json.loads(await _dispatch_experiments_handler({"node_keys": ["1"]}, **kwargs))
    assert "budget" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_refuses_non_pending(dispatch_env):
    store, run_id, _pool, kwargs = dispatch_env
    await store.update_node(run_id, "1", status="running")
    out = json.loads(await _dispatch_experiments_handler({"node_keys": ["1"]}, **kwargs))
    assert "pending" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_refuses_non_leaf(dispatch_env):
    store, run_id, org_id_pool, kwargs = dispatch_env
    # Give node "1" a child so it is no longer a leaf.
    run = await store.get_run(run_id)
    await store.add_node(run_id, org_id=run.org_id, parent_key="1", hypothesis="child")
    out = json.loads(await _dispatch_experiments_handler({"node_keys": ["1"]}, **kwargs))
    assert "leaf" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_creates_worktree_brief_and_task(dispatch_env):
    store, run_id, pool, kwargs = dispatch_env
    run = await store.get_run(run_id)
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], run.org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": ["1"]}, **kwargs,
        ))
    assert out["dispatched"] == ["1"]

    node = await store.get_node(run_id, "1")
    assert node.status == "running"
    assert node.task_id is not None
    assert node.code_ref and node.code_ref.startswith("research/run1/")
    assert node.dispatched_at is not None

    # The repo is bundled and the base64 written to the durable channel
    # the executor reads (terminal-created git state never crosses).
    assert any("git bundle create" in i for (_, _, i) in pool.calls)
    assert any("repo.bundle.b64" in i for (_, _, i) in pool.calls)

    # Brief renders the dev command but NEVER the held-out test command.
    brief = spawn.call_args.kwargs["goal"]
    assert "eval.py --split dev" in brief
    assert "eval.py --split test" not in brief
    assert spawn.call_args.kwargs["max_attempts"] == 1
    assert spawn.call_args.kwargs["agent_def_name"] == "arbor-executor"


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_refuses_without_eval_cmd(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/run2/trunk", branch_prefix="research/run2",
        objective="o",
    )
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h")
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id),
        "sandbox_pool": FakeSandboxPool(),
        "session_store": _StubSessionStore(),
        "tenant": object(), "redis": object(),
    }
    out = json.loads(await _dispatch_experiments_handler({"node_keys": ["1"]}, **kwargs))
    assert "eval_cmd" in out["error"]


# ---------------------------------------------------------------------------
# merge_experiment
# ---------------------------------------------------------------------------


async def _merge_run(session_factory, seeded, *, prefix, with_test_eval):
    org_id, mission_id, session_id = seeded
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch=f"{prefix}/trunk", branch_prefix=prefix, objective="o",
    )
    meta = {"eval_cmd": "python eval.py --split dev"}
    if with_test_eval:
        meta["eval_cmd_test"] = "python eval.py --split test"
    await store.set_meta(run_id, meta)
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h1")
    await store.update_node(
        run_id, "1", status="done", score=0.55,
        code_ref=f"{prefix}/n1-h1-abcd1234",
    )
    pool = FakeSandboxPool()
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id),
        "sandbox_pool": pool,
        "session_store": _StubSessionStore(),
        "tenant": object(), "redis": object(),
    }
    return store, run_id, pool, kwargs


@pytest_asyncio.fixture(loop_scope="session")
async def merge_env(session_factory, seeded_org_and_session):
    """A done node '1' but NO eval_cmd_test configured."""
    return await _merge_run(
        session_factory, seeded_org_and_session,
        prefix="research/mrg1", with_test_eval=False,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def merge_env_with_eval(session_factory, seeded_org_and_session):
    """A done node '1' WITH eval_cmd_test; result.json absent until scripted."""
    return await _merge_run(
        session_factory, seeded_org_and_session,
        prefix="research/mrg2", with_test_eval=True,
    )


def test_merge_schema_accepts_no_score_argument():
    from surogates.tools.builtin.arbor import _MERGE_SCHEMA

    props = _MERGE_SCHEMA.parameters["properties"]
    assert "score" not in props and "test_score" not in props


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_requires_eval_cmd_test(merge_env):
    _store, _run_id, _pool, kwargs = merge_env
    out = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "1"}, **kwargs,
    ))
    assert "eval_cmd_test" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_blocks_on_no_improvement(merge_env_with_eval):
    store, run_id, pool, kwargs = merge_env_with_eval
    await store.set_meta(run_id, {"test_baseline_score": 0.50}, allow_machine_keys=True)
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    pool.responses["cat /workspace/.arbor/merge-eval/1/result.json"] = '{"score": 0.40}'
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is False and out["test_score"] == 0.40
    run = await store.get_run(run_id)
    assert run.meta.get("test_trunk_score") is None  # gate did NOT write


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_merges_on_improvement_and_writes_score(merge_env_with_eval):
    store, run_id, pool, kwargs = merge_env_with_eval
    await store.set_meta(run_id, {"test_baseline_score": 0.50}, allow_machine_keys=True)
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    pool.responses["cat /workspace/.arbor/merge-eval/1/result.json"] = '{"score": 0.61}'
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is True and out["test_score"] == 0.61
    run = await store.get_run(run_id)
    assert run.meta["test_trunk_score"] == 0.61
    assert (await store.get_node(run_id, "1")).status == "merged"
    assert any("git merge --no-ff" in i for (_, _, i) in pool.calls)


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_reports_stale_eval(merge_env_with_eval):
    store, run_id, pool, kwargs = merge_env_with_eval
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    # Rewind started_at far past eval_timeout + grace; result.json stays absent.
    run = await store.get_run(run_id)
    stamp = dict(run.meta["merge_eval"])
    stamp["started_at"] = "2000-01-01T00:00:00+00:00"
    await store.set_meta(run_id, {"merge_eval": stamp}, allow_machine_keys=True)
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out.get("stale") is True


# ---------------------------------------------------------------------------
# idea_tree(record_from_task) — the coordinator's correction channel, which
# resolves the node from the idea_nodes.task_id link and folds via harvest.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_record_from_task_folds_a_real_task(session_factory, seeded_org_and_session):
    from surogates.db.models import Task

    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/rec/trunk", branch_prefix="research/rec",
        objective="o",
    )
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h1")

    # A real terminal Task carrying a structured report, linked to node "1".
    async with session_factory() as db:
        task = Task(
            org_id=org_id, parent_session_id=session_id,
            agent_def_name="arbor-executor", goal="g", status="done",
            max_attempts=1,
            result="prose", result_metadata={"score": 0.73, "insight": "X helps"},
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id
    await store.update_node(run_id, "1", status="running", task_id=task_id)

    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id),
        "sandbox_pool": FakeSandboxPool(),
        "session_store": _StubSessionStore(),
        "tenant": object(), "redis": object(),
    }
    out = json.loads(await _idea_tree_handler(
        {"action": "record_from_task", "task_id": str(task_id)}, **kwargs,
    ))
    assert out["folded"] == "1"
    node = await store.get_node(run_id, "1")
    assert node.status == "done" and node.score == 0.73
    # Insight propagated up to ROOT.
    root = await store.get_node(run_id, "ROOT")
    assert "X helps" in (root.insight or "")


def _worktree_add_cmds(pool):
    return [i for (_, _, i) in pool.calls if "git worktree add --detach" in i]


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_start_prunes_before_adding_worktree(merge_env_with_eval):
    # Each merge-eval launch must `git worktree prune` before `git worktree
    # add`, so a re-start after a stale eval (which rm-ed the dir but left the
    # git admin entry) does not fail as already-registered.
    store, run_id, pool, kwargs = merge_env_with_eval
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    adds = _worktree_add_cmds(pool)
    assert adds, "no worktree-add command issued"
    assert "git worktree prune" in adds[-1]
    assert adds[-1].index("git worktree prune") < adds[-1].index("git worktree add")


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_restart_after_stale_eval_succeeds(merge_env_with_eval):
    store, run_id, pool, kwargs = merge_env_with_eval
    out = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "1"}, **kwargs,
    ))
    assert out["started"] == "1"
    # result.json never appears; rewind started_at so status reports stale.
    run = await store.get_run(run_id)
    stamp = dict(run.meta["merge_eval"])
    stamp["started_at"] = "2000-01-01T00:00:00+00:00"
    await store.set_meta(run_id, {"merge_eval": stamp}, allow_machine_keys=True)
    assert json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    )).get("stale") is True
    # The coordinator re-starts; the second launch must succeed (prune clears
    # the stale admin entry) and re-issue the worktree add.
    before = len(_worktree_add_cmds(pool))
    out2 = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "1"}, **kwargs,
    ))
    assert out2["started"] == "1"
    assert len(_worktree_add_cmds(pool)) == before + 1


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_start_refuses_while_another_eval_in_flight(merge_env_with_eval):
    # The single merge_eval stamp must not be clobbered by a second start for
    # a different node — that would orphan the first node's detached eval.
    store, run_id, pool, kwargs = merge_env_with_eval
    org = (await store.get_run(run_id)).org_id
    await store.add_node(run_id, org_id=org, parent_key="ROOT", hypothesis="second")
    await store.update_node(
        run_id, "2", status="done", score=0.6, code_ref="research/mrg2/n2-x-bbbb2222",
    )
    out1 = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "1"}, **kwargs,
    ))
    assert out1["started"] == "1"
    out2 = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "2"}, **kwargs,
    ))
    assert "merged" not in out2 and "1" in out2["error"]  # refused, names node 1
    # Node 1's eval stamp is untouched.
    run = await store.get_run(run_id)
    assert run.meta["merge_eval"]["node_key"] == "1"


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_removes_worktree_after_consuming_result(merge_env_with_eval):
    store, run_id, pool, kwargs = merge_env_with_eval
    await store.set_meta(run_id, {"test_baseline_score": 0.50}, allow_machine_keys=True)
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    pool.responses["cat /workspace/.arbor/merge-eval/1/result.json"] = '{"score": 0.61}'
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is True
    # Once the score is read the detached eval worktree is dropped (no leak
    # across distinct merged nodes).
    assert any("git worktree remove --force" in i for (_, _, i) in pool.calls)


# ---------------------------------------------------------------------------
# idea_tree forgiving-contract hardening
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_idea_tree_add_normalizes_root_aliases(dispatch_env):
    """parent_key "0" / "root" / omitted all resolve to ROOT, so a
    first-cycle add never bounces on "node '0' not found"."""
    store, run_id, _pool, kwargs = dispatch_env
    for parent in ("0", "root", "Root", None):
        args = {"action": "add", "hypothesis": "Mechanism: x\nHypothesis: y"}
        if parent is not None:
            args["parent_key"] = parent
        out = json.loads(await _idea_tree_handler(args, **kwargs))
        assert "node_key" in out and out["depth"] == 1, out


@pytest.mark.asyncio(loop_scope="session")
async def test_idea_tree_update_aliases_lesson_and_strips_machine_fields(dispatch_env):
    """`lesson` maps to `insight`; score/test_score/branch are reported as
    ignored (set by dispatch/merge, never coordinator prose)."""
    store, run_id, _pool, kwargs = dispatch_env
    out = json.loads(await _idea_tree_handler(
        {"action": "update", "node_key": "1", "fields": {
            "lesson": "keyword lexicon wins",
            "score": 1.0, "test_score": 1.0, "branch": "exp/1",
            "status": "done",
        }},
        **kwargs,
    ))
    assert out["ok"] is True
    assert sorted(out["ignored"]) == ["branch", "score", "test_score"]
    node = await store.get_node(run_id, "1")
    assert node.insight == "keyword lexicon wins"
    assert node.status == "done"


@pytest.mark.asyncio(loop_scope="session")
async def test_idea_tree_set_meta_drops_creation_time_keys(dispatch_env):
    """baseline + repo are fixed at run creation; set_meta reports them as
    ignored and still applies the valid run-config keys."""
    store, run_id, _pool, kwargs = dispatch_env
    out = json.loads(await _idea_tree_handler(
        {"action": "set_meta", "values": {
            "baseline": 0.5, "repo": "/workspace/repo",
            "max_parallel": 3,
        }},
        **kwargs,
    ))
    assert out["ok"] is True
    assert sorted(out["ignored"]) == ["baseline", "repo"]
    run = await store.get_run(run_id)
    assert run.meta["max_parallel"] == 3


# ---------------------------------------------------------------------------
# Bundle-aware task spawn (arbor-executor lives only in the agent bundle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_threads_bundle_to_spawn(dispatch_env):
    """The coordinator's wake bundle (carrying the arbor-executor AgentDef)
    must reach create_task_and_spawn — without it the spawn resolver is
    bundle-blind and every executor dispatch fails."""
    store, run_id, _pool, kwargs = dispatch_env
    run = await store.get_run(run_id)
    sentinel = object()
    kwargs = {**kwargs, "bundle": sentinel}
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], run.org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        await _dispatch_experiments_handler({"node_keys": ["1"]}, **kwargs)
    assert spawn.call_args.kwargs["bundle"] is sentinel


@pytest.mark.asyncio(loop_scope="session")
async def test_create_session_for_task_passes_bundle_to_resolver():
    """_create_session_for_task forwards the bundle to resolve_agent_by_name
    so bundle-delivered sub-agents (arbor-executor) actually resolve."""
    from types import SimpleNamespace

    from surogates.tasks import spawn as spawn_mod

    sentinel_bundle = object()
    captured: dict = {}

    async def fake_resolve(name, tenant, *, session_factory=None, bundle=None, **_):
        captured["bundle"] = bundle
        captured["name"] = name
        return SimpleNamespace(
            name=name, model=None, max_iterations=None, policy_profile=None,
            tools=None, disallowed_tools=None,
            preloaded_skills=["arbor-executor"],
        )

    parent = SimpleNamespace(id=uuid.uuid4(), agent_id="agent-x")
    child = SimpleNamespace(id=uuid.uuid4())
    task = SimpleNamespace(
        id=uuid.uuid4(), agent_def_name="arbor-executor", goal="g",
        context=None, attempt_count=0, parent_session_id=parent.id,
    )
    store = SimpleNamespace(
        get_session=AsyncMock(return_value=parent), emit_event=AsyncMock(),
    )
    with patch.object(spawn_mod, "resolve_agent_by_name", new=fake_resolve), \
        patch.object(
            spawn_mod, "create_child_session",
            new=AsyncMock(return_value=child),
        ), \
        patch(
            "surogates.board.groups.ensure_group_and_inherit",
            new=AsyncMock(),
        ):
        await spawn_mod._create_session_for_task(
            task, session_store=store, session_factory=None,
            tenant=object(), bundle=sentinel_bundle,
        )
    assert captured["bundle"] is sentinel_bundle
    assert captured["name"] == "arbor-executor"


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_eval_extractor_written_where_eval_runs_it(merge_env_with_eval):
    """The detached held-out eval runs ``python3 <extractor>``; the
    extractor must be written to that exact path or the merge gate reads
    no score and nothing ever merges (the .arbor/ vs root path bug)."""
    _store, _run_id, pool, kwargs = merge_env_with_eval
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)

    writes = [
        json.loads(i) for (_, name, i) in pool.calls if name == "write_file"
    ]
    extractor_paths = [
        w["path"] for w in writes
        if str(w.get("path", "")).endswith("extract_score.py")
    ]
    assert extractor_paths == ["/workspace/.arbor/extract_score.py"], (
        extractor_paths
    )
    # The launched eval references the SAME absolute path it was written to.
    assert any(
        "python3 /workspace/.arbor/extract_score.py" in i
        for (_, _, i) in pool.calls
    ), "eval command does not run the extractor at its written path"


def test_terminal_stdout_unwraps_envelope():
    """_terminal_stdout pulls stdout from the terminal tool's JSON
    envelope and passes a bare (non-envelope) string through."""
    from surogates.tools.builtin.arbor import _terminal_stdout

    env = json.dumps({"output": '{"score": 1.0}\n', "exit_code": 0, "error": None})
    assert _terminal_stdout(env) == '{"score": 1.0}\n'
    # bare stdout (e.g. a test stub) is returned unchanged
    assert _terminal_stdout('{"score": 1.0}') == '{"score": 1.0}'
    # empty / missing file
    assert _terminal_stdout(json.dumps({"output": "", "exit_code": 0, "error": None})) == ""
    assert _terminal_stdout("") == ""


@pytest.mark.asyncio(loop_scope="session")
async def test_merge_status_reads_score_from_terminal_envelope(merge_env_with_eval):
    """The real terminal tool wraps stdout in {output, exit_code, error};
    merge status must read result.json from the envelope's output, not
    parse the wrapper (whose null ``error`` masqueraded as 'no score' and
    failed every merge)."""
    store, run_id, pool, kwargs = merge_env_with_eval
    await store.set_meta(
        run_id, {"test_baseline_score": 0.50}, allow_machine_keys=True,
    )
    await _merge_experiment_handler({"action": "start", "node_key": "1"}, **kwargs)
    pool.responses["cat /workspace/.arbor/merge-eval/1/result.json"] = json.dumps(
        {"output": '{"score": 0.61}\n', "exit_code": 0, "error": None},
    )
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is True and out["test_score"] == 0.61
    assert (await store.get_node(run_id, "1")).status == "merged"


# ---------------------------------------------------------------------------
# Bundle handoff (executors run in a separate sandbox; git state can't cross)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_bundle_branch_b64_success_and_failure():
    """_bundle_branch_b64 returns the one-line base64 on success and None
    when the bundle command empties out or leaks a git error."""
    from surogates.tools.builtin.arbor import _bundle_branch_b64

    class _Pool:
        def __init__(self, resp):
            self.resp = resp

        async def ensure(self, *a, **k):
            return None

        async def execute(self, *a, **k):
            return self.resp

    async def call(resp):
        return await _bundle_branch_b64(
            {"sandbox_pool": _Pool(resp), "session_id": "s"},
            repo_path="/repo", trunk_branch="trunk", branch="b", key="1",
        )

    assert await call("ZmFrZS1idW5kbGU=") == "ZmFrZS1idW5kbGU="
    assert await call("") is None                       # bundle failed
    assert await call("fatal: not a git repository") is None  # error leaked


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_writes_bundle_and_brief_clones(dispatch_env):
    """Dispatch persists the repo bundle to the durable path and the brief
    tells the executor to clone it (no pre-made worktree)."""
    store, run_id, pool, kwargs = dispatch_env
    run = await store.get_run(run_id)
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], run.org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        await _dispatch_experiments_handler({"node_keys": ["1"]}, **kwargs)

    writes = [
        json.loads(i) for (_, name, i) in pool.calls if name == "write_file"
    ]
    bundle_writes = [
        w for w in writes
        if str(w.get("path", "")).endswith("repo.bundle.b64")
    ]
    assert bundle_writes, "bundle was not persisted to the durable channel"
    assert bundle_writes[0]["content"] == "ZmFrZS1idW5kbGU="

    brief = spawn.call_args.kwargs["goal"]
    assert "git clone" in brief and "repo.bundle.b64" in brief
    assert "git worktree" not in brief  # no pre-made worktree handoff
