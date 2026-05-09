from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.session.provisioning import create_agent_session


@pytest.mark.asyncio
async def test_create_agent_session_populates_storage_and_model_metadata():
    session_id = uuid4()
    org_id = uuid4()
    user_id = uuid4()
    created = SimpleNamespace(id=session_id)
    store = SimpleNamespace(create_session=AsyncMock(return_value=created))
    storage = SimpleNamespace(
        create_bucket=AsyncMock(),
        resolve_workspace_path=lambda bucket, sid: f"/workspace/{bucket}/{sid}",
    )
    settings = SimpleNamespace(storage=SimpleNamespace(bucket="tenant-bucket"))

    session = await create_agent_session(
        store=store,
        storage=storage,
        settings=settings,
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        channel="web",
        model="gpt-4o",
        config={"system": "be useful"},
        session_id=session_id,
    )

    assert session is created
    storage.create_bucket.assert_awaited_once_with("tenant-bucket")
    call = store.create_session.await_args.kwargs
    assert call["session_id"] == session_id
    assert call["org_id"] == org_id
    assert call["user_id"] == user_id
    assert call["agent_id"] == "agent-a"
    assert call["channel"] == "web"
    assert call["model"] == "gpt-4o"
    assert call["config"]["system"] == "be useful"
    assert call["config"]["storage_bucket"] == "tenant-bucket"
    assert call["config"]["workspace_path"] == f"/workspace/tenant-bucket/{session_id}"
    assert call["config"]["supports_vision"] is True
