"""Integration tests for CodingAgentCredentials against real PostgreSQL."""

from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from surogates.coding_agents.credentials import (
    CodingAgentCredentials,
    CredentialBundle,
)
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def creds(session_factory) -> CodingAgentCredentials:
    vault = CredentialVault(session_factory, Fernet.generate_key())
    return CodingAgentCredentials(vault)


async def test_store_load_status(creds, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    bundle = CredentialBundle(
        provider="anthropic", auth_mode="oauth",
        token_kind="setup_token", oauth_token="sk-ant-oat01-abc",
    )
    await creds.store(org_id=org_id, user_id=user_id, bundle=bundle)

    loaded = await creds.load(org_id=org_id, user_id=user_id, provider="anthropic")
    assert loaded == bundle

    statuses = await creds.statuses(org_id=org_id, user_id=user_id)
    by_provider = {s["provider"]: s for s in statuses}
    assert by_provider["anthropic"]["connected"] is True
    assert by_provider["openai"]["connected"] is False


async def test_no_org_fallback(creds, session_factory):
    """A user with no credential must NOT see an org-scoped one."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # Store an org-scoped credential (user_id=None) directly via the vault.
    await creds._vault.store(
        org_id, "code_cred:anthropic",
        CredentialBundle(provider="anthropic", auth_mode="api_key",
                         api_key="sk-ant-api03-org").to_json(),
    )

    loaded = await creds.load(org_id=org_id, user_id=user_id, provider="anthropic")
    assert loaded is None  # never falls back to the org row


async def test_service_account_principal_stores_and_resolves(creds, session_factory):
    """A service-account principal stores an FK-safe org-scoped row (the SA
    id rides in the name) and resolves it back — and stays isolated from a
    user's credential in the same org."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    sa_id = uuid4()

    await creds.store(
        org_id=org_id, service_account_id=sa_id,
        bundle=CredentialBundle(provider="anthropic", auth_mode="api_key",
                                api_key="sk-ant-api03-sa"),
    )
    loaded = await creds.load(
        org_id=org_id, provider="anthropic", service_account_id=sa_id,
    )
    assert loaded is not None
    assert loaded.api_key == "sk-ant-api03-sa"

    # A user in the same org sees nothing under their own scope.
    assert await creds.load(
        org_id=org_id, provider="anthropic", user_id=user_id,
    ) is None

    statuses = {s["provider"]: s for s in
                await creds.statuses(org_id=org_id, service_account_id=sa_id)}
    assert statuses["anthropic"]["connected"] is True

    assert await creds.delete(
        org_id=org_id, provider="anthropic", service_account_id=sa_id,
    ) is True
    assert await creds.load(
        org_id=org_id, provider="anthropic", service_account_id=sa_id,
    ) is None


async def test_delete(creds, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    await creds.store(
        org_id=org_id, user_id=user_id,
        bundle=CredentialBundle(provider="openai", auth_mode="api_key",
                                api_key="sk-proj-abc"),
    )
    assert await creds.delete(org_id=org_id, user_id=user_id, provider="openai") is True
    assert await creds.load(org_id=org_id, user_id=user_id, provider="openai") is None
    assert await creds.delete(org_id=org_id, user_id=user_id, provider="openai") is False
