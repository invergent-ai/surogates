from __future__ import annotations

from uuid import uuid4

import pytest

from surogates.mcp_proxy.loader import _retrieve_credential


class _RecordingVault:
    def __init__(self, values):
        self.values = values
        self.calls = []

    async def resolve_ref(self, ref, **kwargs):
        self.calls.append((ref, kwargs))
        key = (
            kwargs.get("org_id"),
            kwargs.get("user_id"),
            kwargs.get("service_account_id"),
        )
        return self.values.get(key)


@pytest.mark.asyncio
async def test_retrieve_credential_uses_service_account_scope_first():
    org_id = uuid4()
    service_account_id = uuid4()
    vault = _RecordingVault({
        (org_id, None, service_account_id): "agent-secret",
        (org_id, None, None): "org-secret",
    })

    value, scope = await _retrieve_credential(
        vault,
        org_id,
        service_account_id,
        "GITHUB_TOKEN",
        is_service_account=True,
    )

    assert value == "agent-secret"
    assert scope == "service_account"
    assert vault.calls == [
        (
            "vault://GITHUB_TOKEN",
            {
                "org_id": org_id,
                "service_account_id": service_account_id,
            },
        )
    ]


@pytest.mark.asyncio
async def test_retrieve_credential_service_account_falls_back_to_org():
    org_id = uuid4()
    service_account_id = uuid4()
    vault = _RecordingVault({
        (org_id, None, None): "org-secret",
    })

    value, scope = await _retrieve_credential(
        vault,
        org_id,
        service_account_id,
        "GITHUB_TOKEN",
        is_service_account=True,
    )

    assert value == "org-secret"
    assert scope == "org"
    assert vault.calls[0][1]["service_account_id"] == service_account_id
    assert vault.calls[1][1]["user_id"] is None
    assert vault.calls[1][1].get("service_account_id") is None


@pytest.mark.asyncio
async def test_retrieve_credential_user_scope_still_uses_user_id():
    org_id = uuid4()
    user_id = uuid4()
    vault = _RecordingVault({
        (org_id, user_id, None): "user-secret",
    })

    value, scope = await _retrieve_credential(
        vault,
        org_id,
        user_id,
        "GITHUB_TOKEN",
        is_service_account=False,
    )

    assert value == "user-secret"
    assert scope == "user"
    assert vault.calls == [
        (
            "vault://GITHUB_TOKEN",
            {
                "org_id": org_id,
                "user_id": user_id,
            },
        )
    ]
