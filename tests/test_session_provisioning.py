"""Session creation preserves workspace and principal boundaries."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.session.models import Session
from surogates.session.provisioning import (
    before_a_child,
    before_child,
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
    assert call["model"] is None
    assert call["config"]["system"] == "be useful"
    assert call["config"]["storage_bucket"] == "tenant-bucket"
    # storage_key_prefix is stamped (empty when settings.storage doesn't set it).
    assert call["config"]["storage_key_prefix"] == ""
    assert call["config"]["workspace_path"] == f"/workspace/tenant-bucket/{session_id}"
    # Vision support is not stamped: it depends on the model, which is
    # unknown until the worker resolves the bundle.
    assert "supports_vision" not in call["config"]


@pytest.mark.asyncio
async def test_create_child_session_inherits_workspace_from_root_parent():
    parent = _make_session(
        config={
            "storage_bucket": "tenant-bucket",
            "storage_key_prefix": "",
            "workspace_path": "/workspace/tenant-bucket/abc",
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
        },
    )

    cfg = store.create_session.await_args.kwargs["config"]
    assert cfg["storage_bucket"] == "parent-bucket"
    assert cfg["storage_key_prefix"] == "p-1/a-1"
    assert cfg["workspace_path"] == "/workspace/parent"


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


@pytest.mark.asyncio
async def test_create_agent_session_pins_managed_channel_workspace_boundary():
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

    await create_agent_session(
        store=store,
        storage=storage,
        settings=settings,
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        channel="slack",
        config={"memory_boundary": "slack:c:G1"},
        session_id=session_id,
    )

    cfg = store.create_session.await_args.kwargs["config"]
    assert cfg["memory_boundary"] == "slack:c:G1"
    assert cfg["workspace_boundary"] == "slack:c:G1"


@pytest.mark.asyncio
async def test_create_agent_session_does_not_pin_non_channel_memory_boundary():
    session_id = uuid4()
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=session_id)))
    storage = SimpleNamespace(
        create_bucket=AsyncMock(),
        resolve_workspace_path=lambda bucket, sid: f"/workspace/{bucket}/{sid}",
    )
    settings = SimpleNamespace(storage=SimpleNamespace(bucket="tenant-bucket"))

    await create_agent_session(
        store=store,
        storage=storage,
        settings=settings,
        org_id=uuid4(),
        user_id=uuid4(),
        agent_id="agent-a",
        channel="web",
        config={"memory_boundary": "slack:c:G1"},
        session_id=session_id,
    )

    cfg = store.create_session.await_args.kwargs["config"]
    assert "workspace_boundary" not in cfg


@pytest.mark.asyncio
async def test_create_child_session_inherits_boundary_fields_from_parent():
    parent = _make_session(
        channel="slack",
        config={
            "storage_bucket": "tenant-bucket",
            "storage_key_prefix": "project/agent",
            "workspace_path": "/workspace",
            "memory_boundary": "slack:c:G1",
            "workspace_boundary": "slack:c:G1",
        },
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(store=store, parent=parent, channel="worker")

    cfg = store.create_session.await_args.kwargs["config"]
    assert cfg["memory_boundary"] == "slack:c:G1"
    assert cfg["workspace_boundary"] == "slack:c:G1"
    assert cfg["sandbox_root_session_id"] == str(parent.id)


@pytest.mark.asyncio
async def test_create_child_session_drops_a_boundary_its_parent_lacks():
    parent = _make_session(
        channel="web",
        config={"storage_bucket": "tenant-bucket", "storage_key_prefix": "project/agent", "workspace_path": "/workspace"},
    )
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))

    await create_child_session(
        store=store, parent=parent, channel="worker",
        config={"memory_boundary": "workstream:x", "workspace_boundary": "workstream:x"},
    )

    cfg = store.create_session.await_args.kwargs["config"]
    assert "memory_boundary" not in cfg and "workspace_boundary" not in cfg


@pytest.mark.asyncio
async def test_create_agent_session_stamps_device_execution_after_the_cloud_workspace():
    device_id = uuid4()
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    storage = SimpleNamespace(
        create_bucket=AsyncMock(),
        resolve_workspace_path=lambda bucket, sid: f"/workspace/{bucket}/{sid}",
    )
    await create_agent_session(
        store=store,
        storage=storage,
        settings=SimpleNamespace(storage=SimpleNamespace(bucket="tenant-bucket")),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_id="a-1",
        channel="web",
        device_id=device_id,
        device_name="Flavius's ThinkPad",
        folder="/home/flavius/notes",
    )
    cfg = store.create_session.await_args.kwargs["config"]
    # The computer's name rides with the chat, so its rows name it in every list.
    assert cfg["execution"] == {"kind": "device", "device_id": str(device_id), "device_name": "Flavius's ThinkPad"}
    assert cfg["workspace_path"] == "/home/flavius/notes"
    # Kept for create_child_session, never used for a session on a device.
    assert cfg["storage_bucket"] == "tenant-bucket"


@pytest.mark.asyncio
async def test_create_agent_session_ignores_caller_supplied_execution():
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    storage = SimpleNamespace(
        create_bucket=AsyncMock(),
        resolve_workspace_path=lambda bucket, sid: f"/workspace/{bucket}/{sid}",
    )
    await create_agent_session(
        store=store,
        storage=storage,
        settings=SimpleNamespace(storage=SimpleNamespace(bucket="tenant-bucket")),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_id="a-1",
        channel="web",
        config={"execution": {"kind": "device", "device_id": str(uuid4())}},
    )
    assert "execution" not in store.create_session.await_args.kwargs["config"]


@pytest.mark.asyncio
async def test_create_child_session_inherits_device_execution():
    execution = {"kind": "device", "device_id": str(uuid4())}
    parent = _make_session(config={
        **_workspace_config(), "execution": execution, "workspace_path": "/home/flavius/notes",
    })
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    await create_child_session(
        store=store,
        parent=parent,
        channel="delegation",
        config={"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/etc"},
    )
    cfg = store.create_session.await_args.kwargs["config"]
    assert cfg["execution"] == execution
    assert cfg["workspace_path"] == "/home/flavius/notes"


@pytest.mark.asyncio
async def test_create_child_session_of_a_cloud_parent_cannot_set_execution():
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    await create_child_session(
        store=store,
        parent=_make_session(config=_workspace_config()),
        channel="delegation",
        config={"execution": {"kind": "device", "device_id": str(uuid4())}},
    )
    assert "execution" not in store.create_session.await_args.kwargs["config"]


@pytest.mark.asyncio
async def test_a_project_threads_helpers_and_theirs_each_work_on_a_copy_of_the_threads_work():
    thread = _make_session(config={**_workspace_config(), "workstream_role": "thread", "workstream_id": "w-1"})
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    await create_child_session(store=store, parent=thread, channel="delegation")
    helper = _make_session(config=store.create_session.await_args.kwargs["config"], parent_id=thread.id)
    await create_child_session(store=store, parent=helper, channel="delegation")
    made = store.create_session.await_args.kwargs
    # A helper's helper too: each on a copy of its own, in a pod of its own, of the thread's work.
    assert (made["config"]["history_thread"], made["config"]["history_project"]) == (str(thread.id), "w-1")
    assert made["config"]["sandbox_root_session_id"] == str(made["session_id"])


@pytest.mark.asyncio
async def test_no_caller_names_a_session_a_threads_helper():
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    storage = SimpleNamespace(
        create_bucket=AsyncMock(),
        resolve_workspace_path=lambda bucket, sid: f"/workspace/{bucket}/{sid}",
    )
    await create_agent_session(
        store=store, storage=storage, settings=SimpleNamespace(storage=SimpleNamespace(bucket="tenant-bucket")),
        org_id=uuid4(), user_id=uuid4(), agent_id="a-1", channel="web", config={"history_thread": "t-1", "history_project": "w-1"},
    )
    assert "history_thread" not in store.create_session.await_args.kwargs["config"]
    # Nor a child of a session that is no thread's.
    await create_child_session(
        store=store, parent=_make_session(), channel="delegation", config={"history_thread": "t-1", "history_project": "w-1"},
    )
    assert "history_thread" not in store.create_session.await_args.kwargs["config"]


@pytest.mark.asyncio
async def test_what_a_step_set_for_the_moment_before_it_starts_a_session_runs_once_the_session_is_sure_to_be_made():
    order: list[str] = []

    async def hands_on() -> None:
        order.append("handed on")

    async def made(**session):
        order.append("made")
        return SimpleNamespace(id=uuid4())

    store = SimpleNamespace(create_session=made)
    with before_a_child(hands_on):
        # A parent this function refuses starts no session: nothing runs for it.
        with pytest.raises(ValueError, match="missing required config fields"):
            await create_child_session(store=store, parent=_make_session(config={}), channel="delegation")
        assert order == []
        # Before each session it does make, and before the session is there to be picked up.
        await create_child_session(store=store, parent=_make_session(), channel="delegation")
        await create_child_session(store=store, parent=_make_session(), channel="worker")
        assert order == ["handed on", "made", "handed on", "made"]
    # Outside the step nothing is set: a session made by a tick, or by another step, runs none of it.
    await create_child_session(store=store, parent=_make_session(), channel="task")
    assert order == ["handed on", "made", "handed on", "made", "made"]


@pytest.mark.asyncio
async def test_a_read_is_ended_before_what_a_step_set_runs_and_left_alone_when_nothing_is_set():
    order: list[str] = []
    reading = SimpleNamespace(rollback=AsyncMock(side_effect=lambda: order.append("read ended")))

    async def hands_on() -> None:
        order.append("handed on")

    # What runs may wait for the project's lock: no transaction is left open through it.
    with before_a_child(hands_on):
        await before_child(reading)
    assert order == ["read ended", "handed on"]
    await before_child(reading)
    assert order == ["read ended", "handed on"]


@pytest.mark.asyncio
async def test_every_session_under_a_project_thread_says_which_thread_and_no_caller_can_say_so_for_it():
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    device = {"kind": "device", "device_id": str(uuid4())}
    for thread in (
        _make_session(config={**_workspace_config(), "workstream_role": "thread", "workstream_id": "w-1"}),
        # On the user's computer a helper has no copy and names no thread to hand back to: it says this all the same.
        _make_session(config={**_workspace_config(), "workstream_role": "thread", "workstream_id": "w-1", "execution": device}),
    ):
        await create_child_session(store=store, parent=thread, channel="delegation")
        helper = _make_session(config=store.create_session.await_args.kwargs["config"], parent_id=thread.id)
        await create_child_session(store=store, parent=helper, channel="worker")
        its_own = store.create_session.await_args.kwargs["config"]
        assert helper.config["under_thread"] == its_own["under_thread"] == str(thread.id)
    # A child of any other session does not, whatever its caller passes.
    await create_child_session(store=store, parent=_make_session(), channel="delegation", config={"under_thread": "t-1"})
    assert "under_thread" not in store.create_session.await_args.kwargs["config"]


@pytest.mark.asyncio
async def test_no_caller_says_a_new_chat_is_under_a_project_thread():
    store = SimpleNamespace(create_session=AsyncMock(return_value=SimpleNamespace(id=uuid4())))
    storage = SimpleNamespace(
        create_bucket=AsyncMock(),
        resolve_workspace_path=lambda bucket, sid: f"/workspace/{bucket}/{sid}",
    )
    await create_agent_session(
        store=store, storage=storage, settings=SimpleNamespace(storage=SimpleNamespace(bucket="tenant-bucket")),
        org_id=uuid4(), user_id=uuid4(), agent_id="a-1", channel="web", config={"under_thread": "t-1"},
    )
    assert "under_thread" not in store.create_session.await_args.kwargs["config"]
