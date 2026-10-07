"""What a project's sessions share: its memory, its instructions, its files and its routines."""

from __future__ import annotations

from uuid import UUID

import pytest

from surogates.harness.prompt import PromptBuilder
from surogates.memory.manager import MemoryManager
from surogates.memory.r2_store import R2MemoryStore
from surogates.orchestrator.worker import _build_r2_memory_keys
from surogates.tenant.context import TenantContext
from tests.test_harness_resilience import _make_harness

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import start
from .test_workstreams import create, master_of, runtime

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Memory keys sit under the agent's storage prefix, which is never empty.
PREFIX = "agents/q3"


@pytest.fixture(autouse=True)
def _prefixed(api):
    runtime(api, storage_key_prefix=PREFIX)


async def remember(api, note: str, session=None) -> None:
    """What the memory tool does in *session*: the harness's client posts the
    note with the session's id.  Without one it is the user's own memory, as
    the memory settings write it."""
    query = "" if session is None else f"?session_id={session.id}"
    response = await api.client.post(
        f"/v1/memory{query}", json={"action": "add", "target": "memory", "content": note}, headers=api.auth(),
    )
    assert response.status_code == 200 and response.json()["success"], response.text


async def memory_listed(api, session=None) -> list[str]:
    query = "" if session is None else f"?session_id={session.id}"
    response = await api.client.get(f"/v1/memory{query}", headers=api.auth())
    assert response.status_code == 200, response.text
    return response.json()["memory"]


async def prompt_of(api, session) -> str:
    """*session*'s system prompt, with the memory its worker loads at a wake."""
    settings = api.app.state.settings
    store = R2MemoryStore(
        backend=api.app.state.storage,
        bucket=settings.storage.memory_bucket or settings.storage.bucket,
        keys=_build_r2_memory_keys(session=session, storage_key_prefix=PREFIX, user_id=str(session.user_id)),
    )
    await store.load_from_r2()
    tenant = TenantContext(
        org_id=api.org_id, user_id=api.user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
    )
    builder = PromptBuilder(tenant, session=session, memory_manager=MemoryManager(store))
    return await _make_harness(prompt_builder=builder)._build_system_prompt(session)


async def test_what_one_thread_remembers_is_in_the_next_threads_prompt(api):
    project = await create(api)
    master = await master_of(api, project)
    first = await start(api, master)
    await remember(api, "The board reads every figure in euros.", first)

    second = await start(api, master, title="Summarise B", goal="Summarise B.pdf.")
    assert "The board reads every figure in euros." in await prompt_of(api, second)
    # The project's Memory settings read it through the master.
    assert await memory_listed(api, master) == ["The board reads every figure in euros."]
    assert await memory_listed(api) == []


async def test_a_project_leaves_the_users_own_memory_out(api):
    await remember(api, "Flavius likes short answers.")
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    assert "Flavius likes short answers." not in await prompt_of(api, master)
    assert "Flavius likes short answers." not in await prompt_of(api, thread)

    chat = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    plain = await api.app.state.session_store.get_session(UUID(chat.json()["id"]))
    assert "Flavius likes short answers." in await prompt_of(api, plain)


async def test_one_projects_memory_is_not_anothers(api):
    first = await master_of(api, await create(api))
    await remember(api, "Use the Q3 template.", first)
    other = await master_of(api, await create(api, name="Budget"))
    assert "Use the Q3 template." not in await prompt_of(api, await start(api, other))
    assert await memory_listed(api, other) == []
