"""Cross-tenant isolation smoke test.

Runs twosessions for two different tenants through the per-session
SessionLLMClients construction and TurnConcurrencyGate counter and
asserts no shared mutable state:

1. Each session's SessionLLMClients holds the tenant's own
   resolved API key — not the other's.
2. Each session's storage_key_prefix is the tenant's own — not the
   other's.
3. The TurnConcurrencyGate counters increment independently for
   each tenant (one tenant exhausting its budget cannot block
   another).

This sits in the integration tier (testcontainers) because it
exercises the real CredentialVault DB round-trip + real Redis INCR.
The unit tier already proves the per-call surface in isolation
(test_session_llm.py + test_turn_gate.py); this test catches a
wire-level regression that mocking would miss.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from surogates.harness.session_llm import build_session_llm_clients
from surogates.runtime import (
    AgentRuntimeContext, LLMEndpoint, TurnConcurrencyGate,
)
from surogates.tenant.credentials import CredentialVault

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def vault(session_factory):
    """Per-test CredentialVault backed by the shared Postgres session
    factory + a fresh Fernet key.  Different from the api app fixtures
    so we exercise the vault's full DB round-trip rather than a mock."""
    return CredentialVault(session_factory, Fernet.generate_key())


async def test_two_tenants_no_credential_or_storage_leak(
    vault, redis_client, session_factory,
):
    """End-to-end isolation contract for two tenants on the
    same worker process state."""
    from .conftest import create_org

    # Real orgs in the DB so the credentials FK
    # (``credentials.org_id`` → ``orgs.id``) is satisfied.
    org_a = await create_org(session_factory)
    org_b = await create_org(session_factory)

    # Vault each tenant's main-LLM key.
    await vault.store(
        org_id=org_a, name="acme-main-key", value="sk-acme",
    )
    await vault.store(
        org_id=org_b, name="globex-main-key", value="sk-globex",
    )

    def _ctx(*, agent_id, org_id, key_name, prefix):
        return AgentRuntimeContext(
            agent_id=agent_id,
            org_id=str(org_id),
            project_id="p",
            enabled=True,
            config_version=1,
            storage_key_prefix=prefix,
            llm_main=LLMEndpoint(
                model="m",
                base_url="https://api.example.com",
                api_key_ref=f"vault://{key_name}",
            ),
        )

    ctx_a = _ctx(
        agent_id="a-acme", org_id=org_a,
        key_name="acme-main-key", prefix=f"{org_a}/a-acme",
    )
    ctx_b = _ctx(
        agent_id="a-globex", org_id=org_b,
        key_name="globex-main-key", prefix=f"{org_b}/a-globex",
    )

    # build_session_llm_clients passes ctx.org_id (str) to
    # vault.resolve_ref → vault.retrieve.  CredentialVault.retrieve
    # accepts both UUID and str via SQLAlchemy coercion (the column
    # is UUID typed); we pass the str form to mirror the worker's
    # call path verbatim.
    bundle_a = await build_session_llm_clients(ctx_a, vault=vault)
    bundle_b = await build_session_llm_clients(ctx_b, vault=vault)

    try:
        # 1 — resolved keys are distinct and each matches its
        # tenant.  AsyncOpenAI stores the api_key on the instance;
        # we read it directly to verify no cross-tenant leak.
        assert bundle_a.main.client.api_key == "sk-acme"
        assert bundle_b.main.client.api_key == "sk-globex"
        assert bundle_a.main.client is not bundle_b.main.client

        # 2 — storage prefixes are distinct. asset_root is derived from
        # ctx.storage_key_prefix, never from a process-wide setting.
        assert ctx_a.storage_key_prefix != ctx_b.storage_key_prefix
        assert str(org_a) in ctx_a.storage_key_prefix
        assert str(org_b) in ctx_b.storage_key_prefix
        assert str(org_a) not in ctx_b.storage_key_prefix
        assert str(org_b) not in ctx_a.storage_key_prefix

        # 3 — gate counters are independent.  Acme exhausts its
        # single-slot budget; Globex still has its own full budget.
        gate = TurnConcurrencyGate(redis_client, default_max=1)
        assert await gate.try_acquire(
            str(org_a), "a-acme", limit=1,
        ) is True
        assert await gate.try_acquire(
            str(org_a), "a-acme", limit=1,
        ) is False
        assert await gate.try_acquire(
            str(org_b), "a-globex", limit=1,
        ) is True
        # Release acme; the gate must be re-acquirable.
        await gate.release(str(org_a), "a-acme")
        assert await gate.try_acquire(
            str(org_a), "a-acme", limit=1,
        ) is True
    finally:
        await bundle_a.aclose()
        await bundle_b.aclose()
