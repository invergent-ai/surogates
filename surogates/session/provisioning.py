from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from surogates.harness.model_metadata import get_model_info
from surogates.sandbox.pool import sandbox_session_key
from surogates.session.models import Session
from surogates.session.store import SessionStore
from surogates.storage.tenant import agent_session_bucket


# Fields that pin a child session to its root's workspace.  Callers must
# not be able to override these via ``config`` -- doing so would silently
# break workspace sharing.
_WORKSPACE_SHARING_FIELDS = (
    "storage_bucket",
    "workspace_path",
    "supports_vision",
)


async def create_agent_session(
    *,
    store: SessionStore,
    storage: Any,
    settings: Any,
    org_id: UUID,
    user_id: UUID | None,
    agent_id: str,
    channel: str,
    model: str,
    config: dict | None = None,
    service_account_id: UUID | None = None,
    parent_id: UUID | None = None,
    idempotency_key: str | None = None,
    session_id: UUID | None = None,
) -> Session:
    sid = session_id or uuid4()
    bucket = agent_session_bucket(settings.storage.bucket)
    await storage.create_bucket(bucket)

    merged_config = dict(config or {})
    merged_config["storage_bucket"] = bucket
    merged_config["workspace_path"] = storage.resolve_workspace_path(bucket, sid)

    model_info = get_model_info(model)
    merged_config["supports_vision"] = (
        model_info.supports_vision if model_info is not None else False
    )
    if service_account_id is not None:
        merged_config["service_account_id"] = str(service_account_id)

    return await store.create_session(
        session_id=sid,
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
        channel=channel,
        model=model,
        config=merged_config,
        parent_id=parent_id,
        service_account_id=service_account_id,
        idempotency_key=idempotency_key,
    )


async def create_child_session(
    *,
    store: SessionStore,
    parent: Session,
    channel: str,
    model: str | None = None,
    config: dict | None = None,
    service_account_id: UUID | None = None,
    idempotency_key: str | None = None,
    session_id: UUID | None = None,
    task_id: UUID | None = None,
) -> Session:
    """Create a session that shares its parent's workspace.

    The child reuses the parent's ``storage_bucket`` and
    ``workspace_path`` and stamps ``sandbox_root_session_id`` to the
    ultimate ancestor (via :func:`sandbox_session_key`).  No new
    ``sessions/{child_id}/`` prefix is allocated on storage; tools
    write into the root's workspace prefix.

    Identity is inherited from *parent*: ``agent_id``, ``org_id``,
    ``user_id``, and ``service_account_id`` (unless explicitly
    overridden via *service_account_id*).  ``model`` falls back to
    ``parent.model`` when not supplied.

    *task_id* links the child to a ``tasks`` row when the spawn is a
    subagent task attempt.  Plain ``spawn_worker`` children pass
    ``None`` (the default), keeping their existing semantics unchanged.
    """
    merged_config = dict(config or {})

    parent_config = parent.config or {}
    missing = [f for f in _WORKSPACE_SHARING_FIELDS if f not in parent_config]
    if missing:
        raise ValueError(
            f"Parent session {parent.id} cannot seed a shared workspace: "
            f"missing required config fields {missing}. The parent must "
            f"have been created via create_agent_session() (or another "
            f"path that populates the workspace)."
        )
    for field in _WORKSPACE_SHARING_FIELDS:
        merged_config[field] = parent_config[field]

    merged_config["sandbox_root_session_id"] = sandbox_session_key(parent)

    effective_service_account_id = (
        service_account_id
        if service_account_id is not None
        else parent.service_account_id
    )
    if effective_service_account_id is not None:
        merged_config["service_account_id"] = str(effective_service_account_id)

    return await store.create_session(
        session_id=session_id,
        user_id=parent.user_id,
        org_id=parent.org_id,
        agent_id=parent.agent_id,
        channel=channel,
        model=model if model is not None else parent.model,
        config=merged_config,
        parent_id=parent.id,
        service_account_id=effective_service_account_id,
        idempotency_key=idempotency_key,
        task_id=task_id,
    )
