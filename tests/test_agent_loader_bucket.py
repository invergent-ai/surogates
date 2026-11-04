"""A sub-agent saved to the tenant bucket must reach the sessions that use it.

The ops Sub-agents page and ``POST /v1/agents`` write to the bucket. The worker
only read the bundle, a local directory and the database, so the page listed
sub-agents no session could see: the coordinator got no ``agent_type``, and a
task naming one would have been refused.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.harness.agent_resolver import resolve_agent_by_name
from surogates.storage.backend import LocalBackend
from surogates.storage.tenant import TenantStorage
from surogates.tools.loader import ResourceLoader

ORG = UUID("00000000-0000-0000-0000-000000000001")
USER = UUID("00000000-0000-0000-0000-000000000002")
BUCKET = "workspaces"


def _agent_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: Does {name}\ntools: [read_file]\n---\n\nBe {name}.\n"


def _tenant(tmp_path, user_id: UUID | None = USER) -> SimpleNamespace:
    return SimpleNamespace(org_id=ORG, user_id=user_id, asset_root=str(tmp_path / "assets"))


@pytest.fixture
def backend(tmp_path, monkeypatch: pytest.MonkeyPatch) -> LocalBackend:
    local = LocalBackend(str(tmp_path / "storage"))
    monkeypatch.setattr(ResourceLoader, "_agent_storage", (local, BUCKET))
    return local


async def _save(backend: LocalBackend, name: str, user_id: UUID | None) -> None:
    await backend.create_bucket(BUCKET)
    await TenantStorage(backend=backend, org_id=ORG, user_id=user_id, bucket=BUCKET).write_agent(
        name, _agent_md(name),
    )


@pytest.mark.asyncio
async def test_a_sub_agent_saved_from_ops_resolves_for_a_user_session(backend, tmp_path) -> None:
    """Ops saves through a service account, so it lands in the shared layer."""
    await _save(backend, "link-verifier", user_id=None)

    agent = await resolve_agent_by_name("link-verifier", _tenant(tmp_path))

    assert agent is not None
    assert agent.source == "org"
    assert agent.tools == ["read_file"]


@pytest.mark.asyncio
async def test_a_service_account_session_sees_the_shared_layer(backend, tmp_path) -> None:
    await _save(backend, "link-verifier", user_id=None)

    agents = await ResourceLoader().load_agents(_tenant(tmp_path, user_id=None))

    assert [a.name for a in agents] == ["link-verifier"]


@pytest.mark.asyncio
async def test_a_user_layer_sub_agent_stays_with_its_user(backend, tmp_path) -> None:
    await _save(backend, "my-reviewer", user_id=USER)

    mine = await ResourceLoader().load_agents(_tenant(tmp_path))
    other = await ResourceLoader().load_agents(
        _tenant(tmp_path, user_id=UUID("00000000-0000-0000-0000-000000000003")),
    )

    assert [a.name for a in mine] == ["my-reviewer"]
    assert other == []


@pytest.mark.asyncio
async def test_a_bucket_outage_leaves_the_other_layers_standing(tmp_path, monkeypatch) -> None:
    class Down:
        async def list_keys(self, *_args, **_kwargs):
            raise ConnectionError("storage unavailable")

    class Bundle:
        async def list(self, _prefix):
            return ["agents/bundled/AGENT.md"]

        async def read_text(self, _path):
            return _agent_md("bundled")

    monkeypatch.setattr(ResourceLoader, "_agent_storage", (Down(), BUCKET))

    agents = await ResourceLoader().load_agents(_tenant(tmp_path), bundle=Bundle())

    assert [a.name for a in agents] == ["bundled"]


@pytest.mark.asyncio
async def test_a_process_with_no_storage_has_no_bucket_layer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ResourceLoader, "_agent_storage", None)

    assert await ResourceLoader().load_agents(_tenant(tmp_path)) == []
