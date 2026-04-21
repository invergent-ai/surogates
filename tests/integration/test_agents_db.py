"""Integration tests for sub-agent DB overlay layers in ResourceLoader.

Exercises the 4-layer merge: platform (filesystem) + user (filesystem) +
org_db + user_db.  The DB layers come from the ``agents`` table.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from surogates.db.models import Agent
from surogates.tenant.context import TenantContext
from surogates.tools.loader import (
    AGENT_SOURCE_ORG_DB,
    AGENT_SOURCE_PLATFORM,
    AGENT_SOURCE_USER,
    AGENT_SOURCE_USER_DB,
    ResourceLoader,
)

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _make_tenant(
    asset_root: str, org_id: UUID, user_id: UUID,
) -> TenantContext:
    return TenantContext(
        org_id=org_id,
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
    )


async def _insert_agent(
    session_factory,
    *,
    org_id: UUID,
    user_id: UUID | None,
    name: str,
    description: str = "",
    system_prompt: str = "",
    config: dict | None = None,
    enabled: bool = True,
) -> UUID:
    """Insert a row into the ``agents`` table, returning its id."""
    agent_id = uuid4()
    async with session_factory() as db:
        db.add(
            Agent(
                id=agent_id,
                org_id=org_id,
                user_id=user_id,
                name=name,
                description=description,
                system_prompt=system_prompt,
                config=config or {},
                enabled=enabled,
            )
        )
        await db.commit()
    return agent_id


# ---------------------------------------------------------------------------
# Single-layer DB load
# ---------------------------------------------------------------------------


async def test_org_db_layer_loads_rows(
    session_factory, tmp_path: Path,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    await _insert_agent(
        session_factory,
        org_id=org_id,
        user_id=None,
        name="org-wide-agent",
        description="org-level",
        system_prompt="You are the org-wide sub-agent.",
        config={
            "tools": ["read_file", "search_files"],
            "disallowed_tools": ["write_file"],
            "model": "claude-sonnet-4-6",
            "max_iterations": 15,
            "policy_profile": "read_only",
            "tags": ["review"],
        },
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    by_name = {a.name: a for a in agents}
    assert "org-wide-agent" in by_name
    a = by_name["org-wide-agent"]
    assert a.source == AGENT_SOURCE_ORG_DB
    assert a.tools == ["read_file", "search_files"]
    assert a.disallowed_tools == ["write_file"]
    assert a.model == "claude-sonnet-4-6"
    assert a.max_iterations == 15
    assert a.policy_profile == "read_only"
    assert a.tags == ["review"]
    assert a.system_prompt == "You are the org-wide sub-agent."


async def test_disabled_db_agents_are_excluded(
    session_factory, tmp_path: Path,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    await _insert_agent(
        session_factory, org_id=org_id, user_id=None,
        name="disabled-agent", enabled=False,
    )
    await _insert_agent(
        session_factory, org_id=org_id, user_id=None,
        name="enabled-agent", enabled=True,
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    names = {a.name for a in agents}
    assert "enabled-agent" in names
    assert "disabled-agent" not in names


# ---------------------------------------------------------------------------
# Four-layer merge precedence:
#   platform < user-files < org-db < user-db
# ---------------------------------------------------------------------------


async def test_user_db_overrides_all_other_layers(
    session_factory, tmp_path: Path,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # Layer 1: platform filesystem
    platform_dir = tmp_path / "platform_agents"
    (platform_dir / "shared").mkdir(parents=True)
    (platform_dir / "shared" / "AGENT.md").write_text(
        "---\nname: shared\ndescription: platform version\n---\nPlatform\n",
        encoding="utf-8",
    )

    # Layer 2: user filesystem
    user_fs_dir = (
        tmp_path / "assets" / str(org_id) / "users" / str(user_id)
        / "agents" / "shared"
    )
    user_fs_dir.mkdir(parents=True)
    (user_fs_dir / "AGENT.md").write_text(
        "---\nname: shared\ndescription: user-fs version\n---\nUserFS\n",
        encoding="utf-8",
    )

    # Layer 3: org DB
    await _insert_agent(
        session_factory, org_id=org_id, user_id=None,
        name="shared", description="org-db version",
        system_prompt="OrgDB",
    )

    # Layer 4: user DB
    await _insert_agent(
        session_factory, org_id=org_id, user_id=user_id,
        name="shared", description="user-db version",
        system_prompt="UserDB",
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(platform_dir),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    shared = [a for a in agents if a.name == "shared"]
    assert len(shared) == 1
    assert shared[0].source == AGENT_SOURCE_USER_DB
    assert shared[0].description == "user-db version"
    assert shared[0].system_prompt == "UserDB"


async def test_org_db_overrides_user_filesystem(
    session_factory, tmp_path: Path,
):
    """Org admin DB entries are final over user bucket files.

    This is the contract: end users cannot override org admin decisions
    via their personal bucket.
    """
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # User filesystem (lower precedence than org_db).
    user_fs_dir = (
        tmp_path / "assets" / str(org_id) / "users" / str(user_id)
        / "agents" / "controlled"
    )
    user_fs_dir.mkdir(parents=True)
    (user_fs_dir / "AGENT.md").write_text(
        "---\nname: controlled\ndescription: user attempt\n---\nUser\n",
        encoding="utf-8",
    )

    # Org DB (higher precedence).
    await _insert_agent(
        session_factory, org_id=org_id, user_id=None,
        name="controlled", description="org admin override",
        system_prompt="Admin",
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    controlled = [a for a in agents if a.name == "controlled"]
    assert len(controlled) == 1
    assert controlled[0].source == AGENT_SOURCE_ORG_DB
    assert controlled[0].description == "org admin override"


async def test_platform_only_when_no_db_or_tenant_files(
    session_factory, tmp_path: Path,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    platform_dir = tmp_path / "platform_agents"
    (platform_dir / "only-platform").mkdir(parents=True)
    (platform_dir / "only-platform" / "AGENT.md").write_text(
        "---\nname: only-platform\ndescription: platform-only\n---\nP\n",
        encoding="utf-8",
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(platform_dir),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    only = [a for a in agents if a.name == "only-platform"]
    assert len(only) == 1
    assert only[0].source == AGENT_SOURCE_PLATFORM


async def test_user_fs_visible_when_no_db_row_exists(
    session_factory, tmp_path: Path,
):
    """A user-filesystem agent with no DB overlay survives the 4-layer merge."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    user_fs_dir = (
        tmp_path / "assets" / str(org_id) / "users" / str(user_id)
        / "agents" / "my-personal"
    )
    user_fs_dir.mkdir(parents=True)
    (user_fs_dir / "AGENT.md").write_text(
        "---\nname: my-personal\ndescription: private\n---\nX\n",
        encoding="utf-8",
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    mine = [a for a in agents if a.name == "my-personal"]
    assert len(mine) == 1
    assert mine[0].source == AGENT_SOURCE_USER


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_agents_scoped_to_org(session_factory, tmp_path: Path):
    """DB rows in another org are invisible."""
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)
    user_a = await create_user(session_factory, org_a)

    # Insert an agent for org_b — should be invisible from org_a's tenant.
    await _insert_agent(
        session_factory, org_id=org_b, user_id=None,
        name="other-org-agent", description="not visible to org_a",
    )
    await _insert_agent(
        session_factory, org_id=org_a, user_id=None,
        name="org-a-agent", description="visible to org_a",
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_a, user_a)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    names = {a.name for a in agents}
    assert "org-a-agent" in names
    assert "other-org-agent" not in names


async def test_user_db_rows_scoped_to_user(
    session_factory, tmp_path: Path,
):
    """A user-specific DB row does not leak into another user's view."""
    org_id = await create_org(session_factory)
    user_a = await create_user(session_factory, org_id)
    user_b = await create_user(session_factory, org_id)

    await _insert_agent(
        session_factory, org_id=org_id, user_id=user_a,
        name="a-only", description="belongs to user_a",
    )
    await _insert_agent(
        session_factory, org_id=org_id, user_id=user_b,
        name="b-only", description="belongs to user_b",
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant_a = _make_tenant(str(tmp_path / "assets"), org_id, user_a)

    async with session_factory() as db:
        agents_a = await loader.load_agents(tenant_a, db_session=db)

    names_a = {a.name for a in agents_a}
    assert "a-only" in names_a
    assert "b-only" not in names_a


# ---------------------------------------------------------------------------
# JSONB config round-trip
# ---------------------------------------------------------------------------


async def test_jsonb_config_round_trip(
    session_factory, tmp_path: Path,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    await _insert_agent(
        session_factory, org_id=org_id, user_id=None,
        name="full-config",
        system_prompt="Body",
        config={
            "tools": ["a", "b", "c"],
            "disallowed_tools": ["x"],
            "model": "claude-opus-4-7",
            "max_iterations": 42,
            "policy_profile": "strict",
            "category": "research",
            "tags": ["alpha", "beta"],
        },
    )

    loader = ResourceLoader(
        platform_skills_dir=str(tmp_path / "skills"),
        platform_mcp_dir=str(tmp_path / "mcp"),
        platform_agents_dir=str(tmp_path / "platform_agents"),
    )
    tenant = _make_tenant(str(tmp_path / "assets"), org_id, user_id)

    async with session_factory() as db:
        agents = await loader.load_agents(tenant, db_session=db)

    a = next(a for a in agents if a.name == "full-config")
    assert a.tools == ["a", "b", "c"]
    assert a.disallowed_tools == ["x"]
    assert a.model == "claude-opus-4-7"
    assert a.max_iterations == 42
    assert a.policy_profile == "strict"
    assert a.category == "research"
    assert a.tags == ["alpha", "beta"]
    assert a.system_prompt == "Body"
