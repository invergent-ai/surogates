from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.session.models import Session
from surogates.session.provisioning import (
    create_agent_session,
    create_child_session,
)


_SENTINEL = object()


def _workspace_config() -> dict:
    """Minimum config a parent must carry to seed a shared child workspace."""
    return {
        "storage_bucket": "tenant-bucket",
        "storage_key_prefix": "",
        "workspace_path": "/workspace/tenant-bucket/parent",
        "supports_vision": False,
    }


def _make_session(
    *,
    config: dict | None = None,
    parent_id=None,
    user_id=_SENTINEL,
    service_account_id=None,
    org_id=_SENTINEL,
    agent_id: str = "agent-a",
    channel: str = "web",
    model: str | None = "gpt-4o",
) -> Session:
    """Build a Session that is workspace-ready by default.

    Tests that need to exercise the missing-workspace-fields path must
    pass an explicit ``config`` (e.g. ``{}`` or one missing the keys).
    """
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4() if user_id is _SENTINEL else user_id,
        service_account_id=service_account_id,
        org_id=uuid4() if org_id is _SENTINEL else org_id,
        agent_id=agent_id,
        channel=channel,
        status="active",
        model=model,
        config=_workspace_config() if config is None else config,
        parent_id=parent_id,
        created_at=now,
        updated_at=now,
    )


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
        model="gpt-5.5",
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
    assert call["model"] == "gpt-5.5"
    assert call["config"]["system"] == "be useful"
    assert call["config"]["storage_bucket"] == "tenant-bucket"
    # storage_key_prefix is stamped (empty when settings.storage doesn't set it).
    assert call["config"]["storage_key_prefix"] == ""
    assert call["config"]["workspace_path"] == f"/workspace/tenant-bucket/{session_id}"
    assert call["config"]["supports_vision"] is True


@pytest.mark.asyncio
async def test_create_child_session_inherits_workspace_from_root_parent():
    parent = _make_session(
        config={
            "storage_bucket": "tenant-bucket",
            "storage_key_prefix": "",
            "workspace_path": "/workspace/tenant-bucket/abc",
            "supports_vision": True,
            "system": "parent-system",  # non-sharing field — not inherited
        },
    )
    created = SimpleNamespace(id=uuid4())
    store = SimpleNamespace(create_session=AsyncMock(return_value=created))

    result = await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
        config={"max_iterations": 5, "streaming": False},
    )

    assert result is created
    call = store.create_session.await_args.kwargs
    assert call["parent_id"] == parent.id
    assert call["org_id"] == parent.org_id
    assert call["user_id"] == parent.user_id
    assert call["agent_id"] == parent.agent_id
    assert call["channel"] == "delegation"
    assert call["model"] == parent.model
    cfg = call["config"]
    assert cfg["storage_bucket"] == "tenant-bucket"
    assert cfg["workspace_path"] == "/workspace/tenant-bucket/abc"
    assert cfg["supports_vision"] is True
    assert cfg["sandbox_root_session_id"] == str(parent.id)
    assert cfg["max_iterations"] == 5
    assert cfg["streaming"] is False
    # Non-sharing parent-config fields are NOT silently inherited.
    assert "system" not in cfg


@pytest.mark.asyncio
async def test_create_child_session_grandchild_preserves_root():
    grandparent_id = uuid4()
    parent = _make_session(
        config={
            "storage_bucket": "b",
            "storage_key_prefix": "",
            "workspace_path": "/workspace/b/root",
            "supports_vision": False,
            "sandbox_root_session_id": str(grandparent_id),
        },
        parent_id=grandparent_id,
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
    )

    cfg = store.create_session.await_args.kwargs["config"]
    assert cfg["sandbox_root_session_id"] == str(grandparent_id)
    assert cfg["workspace_path"] == "/workspace/b/root"


@pytest.mark.asyncio
async def test_create_child_session_rejects_parent_missing_workspace_fields():
    """A parent that lacks workspace fields cannot seed a shared child.

    Silently producing a child without storage_bucket / workspace_path
    would disable the workspace governance gate (which only fires when
    workspace_path is set) — exactly the silent failure mode this
    change is meant to close.  Fail loud at child-creation time.
    """
    parent = _make_session(config={"some_other_key": "x"})
    store = SimpleNamespace(create_session=AsyncMock())

    with pytest.raises(ValueError, match="missing required config fields"):
        await create_child_session(
            store=store,
            parent=parent,
            channel="delegation",
        )

    store.create_session.assert_not_called()


@pytest.mark.asyncio
async def test_create_child_session_caller_cannot_override_workspace_fields():
    parent = _make_session(
        config={
            "storage_bucket": "parent-bucket",
            "storage_key_prefix": "p-1/a-1",
            "workspace_path": "/workspace/parent",
            "supports_vision": True,
        },
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store,
        parent=parent,
        channel="worker",
        config={
            "storage_bucket": "attacker-bucket",
            "storage_key_prefix": "p-evil/a-evil",
            "workspace_path": "/elsewhere",
            "supports_vision": False,
        },
    )

    cfg = store.create_session.await_args.kwargs["config"]
    assert cfg["storage_bucket"] == "parent-bucket"
    assert cfg["storage_key_prefix"] == "p-1/a-1"
    assert cfg["workspace_path"] == "/workspace/parent"
    assert cfg["supports_vision"] is True


@pytest.mark.asyncio
async def test_create_child_session_inherits_service_account_from_parent():
    sa_id = uuid4()
    parent = _make_session(
        user_id=None,
        service_account_id=sa_id,
        config={
            "storage_bucket": "b",
            "storage_key_prefix": "",
            "workspace_path": "/w",
            "supports_vision": False,
            "service_account_id": str(sa_id),
        },
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
    )

    call = store.create_session.await_args.kwargs
    assert call["service_account_id"] == sa_id
    assert call["user_id"] is None
    assert call["config"]["service_account_id"] == str(sa_id)


@pytest.mark.asyncio
async def test_create_child_session_explicit_service_account_overrides_parent():
    parent_sa = uuid4()
    override_sa = uuid4()
    parent = _make_session(
        user_id=None,
        service_account_id=parent_sa,
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
        service_account_id=override_sa,
    )

    call = store.create_session.await_args.kwargs
    assert call["service_account_id"] == override_sa
    assert call["config"]["service_account_id"] == str(override_sa)


@pytest.mark.asyncio
async def test_create_child_session_model_falls_back_to_parent():
    parent = _make_session(model="claude-sonnet-4-6")
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store,
        parent=parent,
        channel="worker",
    )

    assert store.create_session.await_args.kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_create_child_session_explicit_model_overrides_parent():
    parent = _make_session(model="claude-sonnet-4-6")
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store,
        parent=parent,
        channel="worker",
        model="gpt-4o",
    )

    assert store.create_session.await_args.kwargs["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_create_child_session_does_not_touch_storage():
    """The helper takes no ``storage`` argument and must not allocate prefixes.

    A real-world bug we want to prevent: a child session triggering
    ``resolve_workspace_path`` or ``create_bucket`` would fragment the
    workspace.  This is asserted structurally: the helper signature does
    not accept ``storage``, and the test passes no such argument.
    """
    parent = _make_session(
        config={
            "storage_bucket": "b",
            "storage_key_prefix": "",
            "workspace_path": "/w",
            "supports_vision": False,
        },
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    # If create_child_session ever needs storage, the import alias
    # would be required to be present in this test's globals.
    await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
    )

    cfg = store.create_session.await_args.kwargs["config"]
    # The helper must reuse the parent's exact workspace_path — not
    # generate a new one.
    assert cfg["workspace_path"] == "/w"


@pytest.mark.asyncio
async def test_create_child_session_propagates_idempotency_and_session_id():
    parent = _make_session()
    explicit_id = uuid4()
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=explicit_id)))

    await create_child_session(
        store=store,
        parent=parent,
        channel="scheduled",
        idempotency_key="scheduled:abc:2026-05-12T00:00:00",
        session_id=explicit_id,
    )

    call = store.create_session.await_args.kwargs
    assert call["session_id"] == explicit_id
    assert call["idempotency_key"] == "scheduled:abc:2026-05-12T00:00:00"
