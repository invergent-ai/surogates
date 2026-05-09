from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from surogates.harness.model_metadata import get_model_info
from surogates.session.models import Session
from surogates.session.store import SessionStore
from surogates.storage.tenant import agent_session_bucket


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
