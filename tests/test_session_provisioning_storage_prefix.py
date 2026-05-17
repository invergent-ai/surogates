"""Verify sessions carry the shared-bucket storage key prefix."""

from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import pytest

from surogates.session.provisioning import (
    create_agent_session,
    create_child_session,
)


class _Store:
    async def create_session(self, **kwargs):
        return SimpleNamespace(**kwargs)


class _Storage:
    async def create_bucket(self, bucket):
        self.bucket = bucket

    def resolve_workspace_path(self, bucket, sid):
        return f"/tmp/{bucket}/{sid}"


@pytest.mark.asyncio
async def test_create_agent_session_stamps_storage_key_prefix():
    # get_model_info is consulted for vision support; stub it so tests
    # don't depend on the model registry.
    with mock.patch(
        "surogates.session.provisioning.get_model_info",
        return_value=SimpleNamespace(supports_vision=False),
    ):
        session = await create_agent_session(
            store=_Store(),
            storage=_Storage(),
            settings=SimpleNamespace(
                storage=SimpleNamespace(
                    bucket="surogate-workspaces",
                    key_prefix="p-1/a-1",
                ),
            ),
            org_id=uuid4(),
            user_id=uuid4(),
            agent_id="a-1",
            channel="api",
            model="gpt-4o",
        )
    assert session.config["storage_bucket"] == "surogate-workspaces"
    assert session.config["storage_key_prefix"] == "p-1/a-1"


@pytest.mark.asyncio
async def test_create_agent_session_defaults_prefix_when_missing():
    """settings.storage with no key_prefix attribute → empty string."""
    with mock.patch(
        "surogates.session.provisioning.get_model_info",
        return_value=SimpleNamespace(supports_vision=False),
    ):
        session = await create_agent_session(
            store=_Store(),
            storage=_Storage(),
            settings=SimpleNamespace(
                storage=SimpleNamespace(bucket="surogate-workspaces"),
            ),
            org_id=uuid4(),
            user_id=uuid4(),
            agent_id="a-1",
            channel="api",
            model="gpt-4o",
        )
    assert session.config["storage_key_prefix"] == ""


@pytest.mark.asyncio
async def test_create_child_session_inherits_storage_key_prefix():
    parent = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_id="a-1",
        model="gpt-4o",
        service_account_id=None,
        config={
            "storage_bucket": "surogate-workspaces",
            "storage_key_prefix": "p-1/a-1",
            "workspace_path": "/workspace",
            "supports_vision": True,
        },
    )
    child = await create_child_session(
        store=_Store(),
        parent=parent,
        channel="api",
    )
    assert child.config["storage_key_prefix"] == "p-1/a-1"


@pytest.mark.asyncio
async def test_create_child_session_rejects_parent_without_prefix():
    """A parent missing storage_key_prefix must fail loudly (workspace sharing invariant)."""
    parent = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        agent_id="a-1",
        model="gpt-4o",
        service_account_id=None,
        config={
            "storage_bucket": "surogate-workspaces",
            "workspace_path": "/workspace",
            "supports_vision": True,
        },
    )
    with pytest.raises(ValueError, match="storage_key_prefix"):
        await create_child_session(
            store=_Store(),
            parent=parent,
            channel="api",
        )
