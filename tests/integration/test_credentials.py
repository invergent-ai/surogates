"""Integration tests for CredentialVault against real PostgreSQL."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fernet_key() -> bytes:
    """Generate a fresh Fernet encryption key per test."""
    return Fernet.generate_key()


@pytest.fixture
def vault(session_factory, fernet_key) -> CredentialVault:
    """CredentialVault backed by the test database."""
    return CredentialVault(session_factory, fernet_key)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_store_and_retrieve(vault, session_factory):
    """Store a credential, retrieve it, verify decrypted value matches."""
    org_id = await create_org(session_factory)

    cred_id, created = await vault.store(org_id, "api-key", "sk-secret-12345")
    assert cred_id is not None
    assert created is True

    value = await vault.retrieve(org_id, "api-key")
    assert value == "sk-secret-12345"


async def test_retrieve_nonexistent(vault, session_factory):
    """Retrieving a credential that does not exist returns None."""
    org_id = await create_org(session_factory)

    value = await vault.retrieve(org_id, "does-not-exist")
    assert value is None


async def test_delete(vault, session_factory):
    """Storing then deleting a credential makes retrieve return None."""
    org_id = await create_org(session_factory)

    await vault.store(org_id, "temp-key", "value-to-delete")
    assert await vault.retrieve(org_id, "temp-key") == "value-to-delete"

    deleted = await vault.delete(org_id, "temp-key")
    assert deleted is True

    assert await vault.retrieve(org_id, "temp-key") is None


async def test_list_names(vault, session_factory):
    """Storing multiple credentials and listing returns all names."""
    org_id = await create_org(session_factory)

    await vault.store(org_id, "key-a", "val-a")
    await vault.store(org_id, "key-b", "val-b")
    await vault.store(org_id, "key-c", "val-c")

    names = await vault.list_names(org_id)
    assert sorted(names) == ["key-a", "key-b", "key-c"]


async def test_org_isolation(vault, session_factory):
    """Credentials stored for org A are not visible to org B."""
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)

    await vault.store(org_a, "shared-name", "org-a-secret")

    # org_a can see it
    assert await vault.retrieve(org_a, "shared-name") == "org-a-secret"

    # org_b cannot
    assert await vault.retrieve(org_b, "shared-name") is None


async def test_store_updates_existing(vault, session_factory):
    """Storing a credential with an existing name updates the value."""
    org_id = await create_org(session_factory)

    _, created = await vault.store(org_id, "rotating-key", "value-v1")
    assert created is True
    assert await vault.retrieve(org_id, "rotating-key") == "value-v1"

    _, created_again = await vault.store(org_id, "rotating-key", "value-v2")
    assert created_again is False
    assert await vault.retrieve(org_id, "rotating-key") == "value-v2"


async def test_user_scoped_credentials(vault, session_factory):
    """User-scoped credentials are isolated from org-level credentials."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # Store org-level and user-level credentials with the same name
    await vault.store(org_id, "api-key", "org-level-value")
    await vault.store(org_id, "api-key", "user-level-value", user_id=user_id)

    # They are independent
    assert await vault.retrieve(org_id, "api-key") == "org-level-value"
    assert await vault.retrieve(org_id, "api-key", user_id=user_id) == "user-level-value"
