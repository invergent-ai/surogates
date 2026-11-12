"""Tests for the ``agent_type`` argument of ``agent``.

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

from tests.test_delegate import _run_agent


def _default_workspace_config() -> dict:
    return {
        "storage_bucket": "tenant-bucket",
        "storage_key_prefix": "",
        "workspace_path": "/workspace/tenant-bucket/parent",
        "supports_vision": False,
    }


def _make_session(**overrides: Any) -> MagicMock:
    """Build a workspace-ready mock session.

    ``create_child_session`` requires the parent to have storage_bucket,
    workspace_path, and supports_vision in its config.  Default to a
    valid set so individual tests don't need to repeat the boilerplate.
    """
    session = MagicMock()
    session.id = overrides.get("id", uuid4())
    session.parent_id = overrides.get("parent_id")
    session.agent_id = overrides.get("agent_id", "agent-test")
    session.model = overrides.get("model", "gpt-4o")
    session.config = overrides.get("config", _default_workspace_config())
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
    enabled: bool = True,
) -> AgentDef:
    return AgentDef(
        name=name, description=description, system_prompt=system_prompt,
        source="platform", tools=tools, disallowed_tools=disallowed_tools,
        model=model, max_iterations=max_iterations,
        enabled=enabled,
    )


# =========================================================================
# agent with agent_type
# =========================================================================


class TestAgentAgentType:

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
                await _run_agent(
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
            result = await _run_agent(
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
            await _run_agent(
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


# =========================================================================
# Shared-workspace inheritance — a sub-agent must take
# the parent's workspace fields onto child sessions.
# =========================================================================


def _parent_with_workspace(
    *,
    sandbox_root_session_id: str | None = None,
) -> MagicMock:
    parent_id = uuid4()
    cfg: dict[str, Any] = {
        "storage_bucket": "agent-a-bucket",
        "storage_key_prefix": "",
        "workspace_path": f"/workspace/agent-a-bucket/sessions/{parent_id}",
        "supports_vision": True,
    }
    if sandbox_root_session_id is not None:
        cfg["sandbox_root_session_id"] = sandbox_root_session_id
    parent = _make_session(id=parent_id, agent_id="agent-a", config=cfg)
    parent.org_id = uuid4()
    parent.user_id = uuid4()
    parent.service_account_id = None
    return parent


class TestSharedWorkspace:

    @pytest.mark.asyncio
    async def test_delegate_child_inherits_parent_workspace(self) -> None:
        from surogates.tools.builtin import delegate

        parent = _parent_with_workspace()
        store = _make_store()
        store.get_session = AsyncMock(return_value=parent)

        with patch.object(
            delegate, "_poll_child_completion",
            AsyncMock(return_value="done"),
        ):
            await _run_agent(
                {"goal": "x"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent.id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        call = store.create_session.call_args[1]
        cfg = call["config"]
        assert cfg["storage_bucket"] == parent.config["storage_bucket"]
        assert cfg["workspace_path"] == parent.config["workspace_path"]
        # Parent is itself a root → root id is parent's own id.
        assert cfg["sandbox_root_session_id"] == str(parent.id)
        # Child identity inherits from parent, NOT from the caller tenant.
        assert call["org_id"] == parent.org_id
        assert call["user_id"] == parent.user_id
        assert call["agent_id"] == parent.agent_id

    @pytest.mark.asyncio
    async def test_delegate_grandchild_preserves_root(self) -> None:
        from surogates.tools.builtin import delegate

        root_id = uuid4()
        # Parent is already a delegation child — its config carries the root.
        parent = _parent_with_workspace(
            sandbox_root_session_id=str(root_id),
        )
        store = _make_store()
        store.get_session = AsyncMock(return_value=parent)

        with patch.object(
            delegate, "_poll_child_completion",
            AsyncMock(return_value="done"),
        ):
            await _run_agent(
                {"goal": "x"},
                session_store=store,
                redis=_make_redis(),
                tenant=MagicMock(user_id=uuid4(), org_id=uuid4()),
                session_id=str(parent.id),
                budget=IterationBudget(max_total=50),
                session_factory=MagicMock(),
            )

        cfg = store.create_session.call_args[1]["config"]
        assert cfg["sandbox_root_session_id"] == str(root_id)
