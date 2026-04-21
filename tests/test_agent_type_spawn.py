"""Tests for ``agent_type`` argument in spawn_worker and delegate_task.

Covers resolution, config hydration, explicit-argument precedence, and
error paths when an unknown or disabled agent type is referenced.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.tools.loader import AgentDef


def _make_session(**overrides: Any) -> MagicMock:
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.parent_id = overrides.get("parent_id")
    session.agent_id = overrides.get("agent_id", "agent-test")
    session.model = overrides.get("model", "gpt-4o")
    session.config = overrides.get("config", {})
    return session


def _make_store() -> AsyncMock:
    store = AsyncMock()
    child = _make_session(id=uuid4(), parent_id=uuid4())
    store.create_session = AsyncMock(return_value=child)
    store.emit_event = AsyncMock(return_value=1)
    store.get_session = AsyncMock(return_value=_make_session())
    store.get_events = AsyncMock(return_value=[])
    return store


def _make_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.zadd = AsyncMock()
    redis.publish = AsyncMock()
    return redis


def _agent(
    *,
    name: str = "code-reviewer",
    description: str = "Reviews code",
    system_prompt: str = "Body.",
    tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    model: str | None = None,
    max_iterations: int | None = None,
    policy_profile: str | None = None,
    enabled: bool = True,
) -> AgentDef:
    return AgentDef(
        name=name, description=description, system_prompt=system_prompt,
        source="platform", tools=tools, disallowed_tools=disallowed_tools,
        model=model, max_iterations=max_iterations,
        policy_profile=policy_profile, enabled=enabled,
    )


# =========================================================================
# spawn_worker with agent_type
# =========================================================================


class TestSpawnWorkerAgentType:

    @pytest.mark.asyncio
    async def test_agent_type_populates_child_config(self) -> None:
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        child_id = uuid4()
        parent = _make_session(id=parent_id, agent_id="agent-1")
        store = _make_store()
        store.create_session = AsyncMock(
            return_value=_make_session(id=child_id, parent_id=parent_id),
        )
        store.get_session = AsyncMock(return_value=parent)

        agent = _agent(
            name="researcher",
            tools=["read_file", "search_files"],
            disallowed_tools=["write_file"],
            model="claude-sonnet-4-6",
            max_iterations=12,
            policy_profile="read_only",
        )

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=agent),
        ):
            result = await coordinator._spawn_worker_handler(
                {"goal": "find prior art", "agent_type": "researcher"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        parsed = json.loads(result)
        assert parsed["status"] == "spawned"

        call_kwargs = store.create_session.call_args[1]
        cfg = call_kwargs["config"]
        assert cfg["agent_type"] == "researcher"
        assert cfg["allowed_tools"] == ["read_file", "search_files"]
        assert cfg["max_iterations"] == 12
        assert cfg["policy_profile"] == "read_only"
        # When allowed_tools is set, we don't emit excluded_tools.
        assert "excluded_tools" not in cfg
        # Model override from the agent def is applied to the child.
        assert call_kwargs["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_explicit_tools_override_agent_def(self) -> None:
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        store = _make_store()
        store.get_session = AsyncMock(
            return_value=_make_session(id=parent_id, agent_id="a"),
        )

        agent = _agent(name="researcher", tools=["from_agent_def"])

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=agent),
        ):
            await coordinator._spawn_worker_handler(
                {
                    "goal": "do it",
                    "agent_type": "researcher",
                    "tools": ["explicit_tool"],
                },
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        cfg = store.create_session.call_args[1]["config"]
        assert cfg["allowed_tools"] == ["explicit_tool"]

    @pytest.mark.asyncio
    async def test_explicit_model_overrides_agent_def(self) -> None:
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        store = _make_store()
        store.get_session = AsyncMock(
            return_value=_make_session(id=parent_id, agent_id="a"),
        )

        agent = _agent(name="r", model="from_agent_def")

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=agent),
        ):
            await coordinator._spawn_worker_handler(
                {
                    "goal": "do it",
                    "agent_type": "r",
                    "model": "gpt-5-explicit",
                },
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        assert store.create_session.call_args[1]["model"] == "gpt-5-explicit"

    @pytest.mark.asyncio
    async def test_unknown_agent_type_returns_error(self) -> None:
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        store = _make_store()

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=None),
        ):
            result = await coordinator._spawn_worker_handler(
                {"goal": "do it", "agent_type": "does-not-exist"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        parsed = json.loads(result)
        assert "error" in parsed
        assert "does-not-exist" in parsed["error"]
        # No child session created when agent_type is unresolvable.
        store.create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_iterations_caps_budget(self) -> None:
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        store = _make_store()
        store.get_session = AsyncMock(
            return_value=_make_session(id=parent_id, agent_id="a"),
        )

        # Agent def says 5 iterations; parent budget is 100. Expect 5.
        agent = _agent(name="tight", max_iterations=5)

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=agent),
        ):
            await coordinator._spawn_worker_handler(
                {"goal": "do it", "agent_type": "tight"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=100),
                session_factory=MagicMock(),
            )

        cfg = store.create_session.call_args[1]["config"]
        assert cfg["max_iterations"] == 5

    @pytest.mark.asyncio
    async def test_agent_def_denylist_merges_with_worker_excluded(self) -> None:
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        store = _make_store()
        store.get_session = AsyncMock(
            return_value=_make_session(id=parent_id, agent_id="a"),
        )

        # No allowlist; denylist adds agent-def exclusions on top of
        # WORKER_EXCLUDED_TOOLS.
        agent = _agent(name="lim", disallowed_tools=["write_file", "patch"])

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=agent),
        ):
            await coordinator._spawn_worker_handler(
                {"goal": "do it", "agent_type": "lim"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        cfg = store.create_session.call_args[1]["config"]
        assert "write_file" in cfg["excluded_tools"]
        assert "patch" in cfg["excluded_tools"]
        # Coordinator-recursion exclusions still present.
        for forbidden in ("spawn_worker", "send_worker_message", "stop_worker"):
            assert forbidden in cfg["excluded_tools"]

    @pytest.mark.asyncio
    async def test_no_agent_type_preserves_legacy_behavior(self) -> None:
        """Ad-hoc spawn (no agent_type) still works exactly as before."""
        from surogates.tools.builtin import coordinator

        parent_id = uuid4()
        store = _make_store()
        store.get_session = AsyncMock(
            return_value=_make_session(id=parent_id, agent_id="a"),
        )

        await coordinator._spawn_worker_handler(
            {"goal": "do it"},
            session_store=store,
            redis=_make_redis(),
            tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
            session_id=str(parent_id),
            budget=IterationBudget(max_total=50),
            session_factory=MagicMock(),
        )

        cfg = store.create_session.call_args[1]["config"]
        assert "agent_type" not in cfg
        assert "allowed_tools" not in cfg
        # Legacy behavior: excluded_tools includes coordinator tools only.
        assert "spawn_worker" in cfg["excluded_tools"]


# =========================================================================
# delegate_task with agent_type
# =========================================================================


class TestDelegateTaskAgentType:

    @pytest.mark.asyncio
    async def test_agent_type_populates_child_config(self) -> None:
        from surogates.tools.builtin import coordinator, delegate

        parent_id = uuid4()
        child_id = uuid4()
        parent = _make_session(id=parent_id, agent_id="agent-1")
        store = _make_store()
        store.create_session = AsyncMock(
            return_value=_make_session(id=child_id, parent_id=parent_id),
        )
        store.get_session = AsyncMock(return_value=parent)

        agent = _agent(
            name="analyzer",
            tools=["read_file"],
            model="claude-opus-4-7",
            max_iterations=8,
            policy_profile="read_only",
        )

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=agent),
        ):
            # Prevent the blocking poll loop from actually waiting.
            with patch.object(
                delegate, "_poll_child_completion",
                AsyncMock(return_value=json.dumps({"ok": True})),
            ):
                await delegate._delegate_handler(
                    {"goal": "analyze", "agent_type": "analyzer"},
                    session_store=store,
                    redis=_make_redis(),
                    tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                    session_id=str(parent_id),
                    budget=IterationBudget(max_total=50),
                    session_factory=MagicMock(),
                )

        call_kwargs = store.create_session.call_args[1]
        cfg = call_kwargs["config"]
        assert cfg["agent_type"] == "analyzer"
        assert cfg["allowed_tools"] == ["read_file"]
        assert cfg["max_iterations"] == 8
        assert cfg["policy_profile"] == "read_only"
        assert call_kwargs["model"] == "claude-opus-4-7"

    @pytest.mark.asyncio
    async def test_unknown_agent_type_returns_error(self) -> None:
        from surogates.tools.builtin import coordinator, delegate

        parent_id = uuid4()
        store = _make_store()

        with patch(
            "surogates.harness.agent_resolver.resolve_agent_by_name",
            AsyncMock(return_value=None),
        ):
            result = await delegate._delegate_handler(
                {"goal": "x", "agent_type": "nope"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        parsed = json.loads(result)
        assert "error" in parsed
        assert "nope" in parsed["error"]
        store.create_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_agent_type_preserves_legacy_behavior(self) -> None:
        from surogates.tools.builtin import delegate

        parent_id = uuid4()
        store = _make_store()
        store.get_session = AsyncMock(
            return_value=_make_session(id=parent_id, agent_id="a"),
        )

        with patch.object(
            delegate, "_poll_child_completion",
            AsyncMock(return_value="done"),
        ):
            await delegate._delegate_handler(
                {"goal": "x"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent_id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        cfg = store.create_session.call_args[1]["config"]
        assert "agent_type" not in cfg
        assert "allowed_tools" not in cfg
        assert "policy_profile" not in cfg
