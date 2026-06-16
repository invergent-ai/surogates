"""Unit tests for the wake-time sub-agent resolver."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from surogates.harness.agent_resolver import (
    apply_agent_def_to_session,
    resolve_agent_def,
)
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.loader import AgentDef, ResourceLoader


def _make_session(
    *,
    agent_type: str | None = None,
    config: dict | None = None,
    model: str | None = None,
) -> Session:
    cfg = dict(config or {})
    if agent_type:
        cfg["agent_type"] = agent_type
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="test-agent",
        channel="worker",
        status="active",
        model=model,
        config=cfg,
        created_at=now,
        updated_at=now,
    )


def _make_tenant(asset_root: str) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
    )


class _FakeBundle:
    """Minimal in-memory stand-in for :class:`AgentFileBundle`.

    Agents now come from the per-tenant Hub bundle's ``agents/<name>/
    AGENT.md`` subtree rather than a configured on-disk directory.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self._files = dict(files)

    async def list(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_text(self, path: str) -> str:
        if path not in self._files:
            raise LookupError(path)
        return self._files[path]


def _agent_md(
    *,
    name: str,
    description: str = "d",
    tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    model: str | None = None,
    max_iterations: int | None = None,
    policy_profile: str | None = None,
    enabled: bool = True,
    body: str = "Body.",
) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    if tools is not None:
        lines.append("tools: [" + ", ".join(tools) + "]")
    if disallowed_tools is not None:
        lines.append("disallowed_tools: [" + ", ".join(disallowed_tools) + "]")
    if model is not None:
        lines.append(f"model: {model}")
    if max_iterations is not None:
        lines.append(f"max_iterations: {max_iterations}")
    if policy_profile is not None:
        lines.append(f"policy_profile: {policy_profile}")
    if not enabled:
        lines.append("enabled: false")
    lines.append("---")
    lines.append(body)
    return "\n".join(lines) + "\n"


def _bundle_with_agent(**agent_kwargs) -> _FakeBundle:
    name = agent_kwargs["name"]
    return _FakeBundle({f"agents/{name}/AGENT.md": _agent_md(**agent_kwargs)})


# =========================================================================
# resolve_agent_def
# =========================================================================


class TestResolveAgentDef:

    @pytest.mark.asyncio
    async def test_returns_none_when_no_agent_type_set(self, tmp_path: Path):
        session = _make_session()
        tenant = _make_tenant(str(tmp_path / "assets"))
        bundle = _FakeBundle({})

        result = await resolve_agent_def(
            session, tenant, loader=ResourceLoader(), bundle=bundle,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_resolves_to_loaded_def(self, tmp_path: Path):
        bundle = _bundle_with_agent(
            name="code-reviewer",
            description="Reviews code",
            tools=["read_file", "search_files"],
            model="claude-sonnet-4-6",
            max_iterations=15,
        )

        session = _make_session(agent_type="code-reviewer")
        tenant = _make_tenant(str(tmp_path / "assets"))

        result = await resolve_agent_def(
            session, tenant, loader=ResourceLoader(), bundle=bundle,
        )
        assert result is not None
        assert result.name == "code-reviewer"
        assert result.description == "Reviews code"
        assert result.tools == ["read_file", "search_files"]
        assert result.model == "claude-sonnet-4-6"
        assert result.max_iterations == 15

    @pytest.mark.asyncio
    async def test_unknown_agent_type_returns_none(self, tmp_path: Path):
        bundle = _bundle_with_agent(name="known")

        session = _make_session(agent_type="does-not-exist")
        tenant = _make_tenant(str(tmp_path / "assets"))

        result = await resolve_agent_def(
            session, tenant, loader=ResourceLoader(), bundle=bundle,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_agent_is_not_resolved(self, tmp_path: Path):
        bundle = _bundle_with_agent(name="off", enabled=False)

        session = _make_session(agent_type="off")
        tenant = _make_tenant(str(tmp_path / "assets"))

        result = await resolve_agent_def(
            session, tenant, loader=ResourceLoader(), bundle=bundle,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_loader_errors_are_swallowed(self, tmp_path: Path):
        """If the loader raises, resolver returns None rather than crashing wake."""

        class BoomLoader:
            async def load_agents(self, tenant, db_session=None):
                raise RuntimeError("simulated loader failure")

        session = _make_session(agent_type="any")
        tenant = _make_tenant(str(tmp_path / "assets"))

        result = await resolve_agent_def(session, tenant, loader=BoomLoader())
        assert result is None


# =========================================================================
# apply_agent_def_to_session
# =========================================================================


class TestApplyAgentDefToSession:

    def _make_def(self, **overrides) -> AgentDef:
        defaults: dict = dict(
            name="x", description="x", system_prompt="body",
            source="platform",
        )
        defaults.update(overrides)
        return AgentDef(**defaults)

    def test_populates_allowed_tools_when_unset(self):
        session = _make_session(agent_type="x")
        agent = self._make_def(tools=["a", "b"])
        apply_agent_def_to_session(session, agent)
        assert session.config["allowed_tools"] == ["a", "b"]

    def test_does_not_overwrite_explicit_allowed_tools(self):
        session = _make_session(
            agent_type="x", config={"allowed_tools": ["explicit"]},
        )
        agent = self._make_def(tools=["from_def"])
        apply_agent_def_to_session(session, agent)
        assert session.config["allowed_tools"] == ["explicit"]

    def test_populates_excluded_tools_when_unset(self):
        session = _make_session(agent_type="x")
        agent = self._make_def(disallowed_tools=["x", "y"])
        apply_agent_def_to_session(session, agent)
        assert session.config["excluded_tools"] == ["x", "y"]

    def test_does_not_overwrite_explicit_excluded_tools(self):
        session = _make_session(
            agent_type="x", config={"excluded_tools": ["e"]},
        )
        agent = self._make_def(disallowed_tools=["from_def"])
        apply_agent_def_to_session(session, agent)
        assert session.config["excluded_tools"] == ["e"]

    def test_populates_max_iterations_when_unset(self):
        session = _make_session(agent_type="x")
        agent = self._make_def(max_iterations=5)
        apply_agent_def_to_session(session, agent)
        assert session.config["max_iterations"] == 5

    def test_does_not_overwrite_explicit_max_iterations(self):
        session = _make_session(
            agent_type="x", config={"max_iterations": 100},
        )
        agent = self._make_def(max_iterations=5)
        apply_agent_def_to_session(session, agent)
        assert session.config["max_iterations"] == 100

    def test_max_iterations_is_capped_at_worker_ceiling(self):
        """An agent def asking for more than the ceiling is clamped down.

        Prevents a webhook-created session with ``config.agent_type`` set
        (which bypasses ``spawn_worker``'s own clamp) from granting itself
        a larger budget than a coordinator-spawned child would receive.
        """
        from surogates.harness.agent_resolver import _MAX_ITERATIONS_CEILING

        session = _make_session(agent_type="x")
        agent = self._make_def(max_iterations=_MAX_ITERATIONS_CEILING * 10)
        apply_agent_def_to_session(session, agent)
        assert session.config["max_iterations"] == _MAX_ITERATIONS_CEILING

    def test_populates_policy_profile_when_unset(self):
        session = _make_session(agent_type="x")
        agent = self._make_def(policy_profile="read_only")
        apply_agent_def_to_session(session, agent)
        assert session.config["policy_profile"] == "read_only"

    def test_does_not_overwrite_explicit_policy_profile(self):
        session = _make_session(
            agent_type="x", config={"policy_profile": "strict"},
        )
        agent = self._make_def(policy_profile="read_only")
        apply_agent_def_to_session(session, agent)
        assert session.config["policy_profile"] == "strict"

    def test_populates_model_when_session_model_is_none(self):
        session = _make_session(agent_type="x", model=None)
        agent = self._make_def(model="claude-opus-4-7")
        apply_agent_def_to_session(session, agent)
        assert session.model == "claude-opus-4-7"

    def test_does_not_overwrite_explicit_session_model(self):
        session = _make_session(agent_type="x", model="gpt-4o")
        agent = self._make_def(model="claude-opus-4-7")
        apply_agent_def_to_session(session, agent)
        assert session.model == "gpt-4o"

    def test_none_fields_on_agent_def_leave_config_untouched(self):
        session = _make_session(agent_type="x")
        agent = self._make_def()  # all optional fields None
        apply_agent_def_to_session(session, agent)
        assert "allowed_tools" not in session.config
        assert "excluded_tools" not in session.config
        assert "max_iterations" not in session.config
        assert "policy_profile" not in session.config


# =========================================================================
# End-to-end: resolve + apply
# =========================================================================


class TestResolveAndApplyEndToEnd:

    @pytest.mark.asyncio
    async def test_full_flow_populates_session(self, tmp_path: Path):
        bundle = _bundle_with_agent(
            name="researcher",
            description="Research tasks",
            tools=["read_file", "search_files", "web_search"],
            disallowed_tools=["write_file"],
            model="claude-sonnet-4-6",
            max_iterations=25,
            policy_profile="read_only",
        )

        session = _make_session(agent_type="researcher")
        tenant = _make_tenant(str(tmp_path / "assets"))

        agent = await resolve_agent_def(
            session, tenant, loader=ResourceLoader(), bundle=bundle,
        )
        assert agent is not None
        apply_agent_def_to_session(session, agent)

        assert session.config["allowed_tools"] == ["read_file", "search_files", "web_search"]
        assert session.config["excluded_tools"] == ["write_file"]
        assert session.config["max_iterations"] == 25
        assert session.config["policy_profile"] == "read_only"
        assert session.model == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_agent_type_unresolved_leaves_session_config_untouched(
        self, tmp_path: Path,
    ):
        bundle = _FakeBundle({})

        session = _make_session(agent_type="nope")
        tenant = _make_tenant(str(tmp_path / "assets"))

        agent = await resolve_agent_def(
            session, tenant, loader=ResourceLoader(), bundle=bundle,
        )
        assert agent is None
        # No apply call when resolve returns None; config stays as-is.
        assert session.config == {"agent_type": "nope"}
