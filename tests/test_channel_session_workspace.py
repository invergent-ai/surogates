"""Channel sessions must be provisioned with a persistent workspace.

A channel (Slack/Telegram) session created with a storage backend + settings
should carry storage_bucket / storage_key_prefix / workspace_path in its config —
the same fields API/web sessions get via create_agent_session — so the worker
mounts a persistent /workspace and inbound attachments can be written there.
Without those fields the worker mounts no workspace and attachments are skipped.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from surogates.channels.identity import get_or_create_channel_session


class _Result:
    def scalar_one_or_none(self):
        return None


class _DB:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _Result()


class _SessionFactory:
    def __call__(self):
        return _DB()


class _Store:
    def __init__(self):
        self.created: dict | None = None

    async def create_session(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(**kwargs)


class _Storage:
    def __init__(self):
        self.created_bucket = None

    async def create_bucket(self, bucket):
        self.created_bucket = bucket

    def resolve_workspace_path(self, bucket, sid):
        return f"/ws/{bucket}/{sid}"


def _settings():
    return SimpleNamespace(
        storage=SimpleNamespace(bucket="surogate-workspaces-dev", key_prefix="p1/a1"),
    )


class TestChannelSessionWorkspace:
    async def test_provisions_workspace_when_storage_and_settings_given(self):
        store = _Store()
        storage = _Storage()
        with mock.patch(
            "surogates.session.provisioning.get_model_info",
            return_value=SimpleNamespace(supports_vision=False),
        ):
            await get_or_create_channel_session(
                store,
                None,
                session_key="agent:slack:dm:D1",
                user_id=uuid4(),
                org_id=uuid4(),
                agent_id="a1",
                channel="slack",
                config={"slack_channel_id": "D1", "memory_boundary": "slack:d:D1"},
                session_factory=_SessionFactory(),
                storage=storage,
                settings=_settings(),
            )
        cfg = store.created["config"]
        sid = store.created["session_id"]
        assert cfg["storage_bucket"] == "surogate-workspaces-dev"
        assert cfg["storage_key_prefix"] == "p1/a1"
        assert cfg["workspace_path"] == f"/ws/surogate-workspaces-dev/{sid}"
        # Channel-specific config is preserved alongside the workspace fields.
        assert cfg["channel_session_key"] == "agent:slack:dm:D1"
        assert cfg["slack_channel_id"] == "D1"
        assert storage.created_bucket == "surogate-workspaces-dev"

    async def test_workspace_provision_failure_degrades_gracefully(self):
        # A storage error while provisioning the workspace (e.g. create_bucket
        # fails, or an empty configured bucket) must NOT abort session creation —
        # otherwise the inbound webhook 500s and the platform retries. The session
        # is created workspace-less and the message still processes.
        class _FailStorage:
            async def create_bucket(self, bucket):
                raise RuntimeError("storage unavailable")

            def resolve_workspace_path(self, bucket, sid):
                return f"/ws/{sid}"

        store = _Store()
        with mock.patch(
            "surogates.session.provisioning.get_model_info",
            return_value=SimpleNamespace(supports_vision=False),
        ):
            sid = await get_or_create_channel_session(
                store,
                None,
                session_key="agent:slack:dm:D2",
                user_id=uuid4(),
                org_id=uuid4(),
                agent_id="a1",
                channel="slack",
                config={"slack_channel_id": "D2"},
                session_factory=_SessionFactory(),
                storage=_FailStorage(),
                settings=_settings(),
            )
        # Session still created (no exception propagated), just without a workspace.
        assert sid == store.created["session_id"]
        cfg = store.created["config"]
        assert "storage_bucket" not in cfg
        assert cfg["channel_session_key"] == "agent:slack:dm:D2"

    async def test_skips_workspace_without_storage_or_settings(self):
        # Backward compat: a caller that does not supply storage+settings creates
        # a session with no workspace fields (the pre-fix behavior).
        store = _Store()
        await get_or_create_channel_session(
            store,
            None,
            session_key="k",
            user_id=uuid4(),
            org_id=uuid4(),
            agent_id="a1",
            channel="slack",
            config={"slack_channel_id": "D1"},
            session_factory=_SessionFactory(),
        )
        cfg = store.created["config"]
        assert "storage_bucket" not in cfg
        assert cfg["channel_session_key"] == "k"
