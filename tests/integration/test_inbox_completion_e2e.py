"""End-to-end tests for task-complete inbox acknowledgements."""

from __future__ import annotations

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.tools.registry import ToolRegistry

from .inbox_e2e_helpers import StubTenant
from .inbox_e2e_helpers import app as app
from .inbox_e2e_helpers import client as client
from .inbox_e2e_helpers import create_user_token_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_completion_inbox_and_ack(
    client,
    session_factory,
    session_store,
):
    user_session = await create_user_token_session(session_factory, session_store)
    await session_store.update_session_title_if_empty(
        user_session.session.id,
        "Migrate users",
    )
    session = await session_store.get_session(user_session.session.id)
    lease = await session_store.try_acquire_lease(
        session.id,
        "worker-completion-e2e",
        ttl_seconds=60,
    )
    assert lease is not None

    harness = AgentHarness(
        session_store=session_store,
        tool_registry=ToolRegistry(),
        llm_client=object(),
        tenant=StubTenant(
            org_id=user_session.org_id,
            user_id=user_session.user_id,
        ),
        worker_id="worker-completion-e2e",
        budget=IterationBudget(max_total=10),
        context_compressor=object(),
        prompt_builder=object(),
    )

    await harness._complete_session(
        session,
        [{"role": "assistant", "content": "All done."}],
        lease,
        reason="completed",
    )

    list_response = await client.get(
        f"/v1/inbox?kind=task_complete&session_id={session.id}",
        headers=user_session.auth_headers,
    )
    assert list_response.status_code == 200, list_response.text
    items = list_response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "task_complete"
    assert item["status"] == "pending"
    assert item["title"] == "Migrate users"
    assert item["body"] == "All done."
    assert item["payload"]["outcome"] == "success"

    response = await client.post(
        f"/v1/inbox/{item['id']}/ack",
        headers=user_session.auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["responded_at"] is not None
