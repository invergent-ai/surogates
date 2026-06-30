"""Tests for resolve_channel_token.

Per-tenant bot tokens live in the credential
vault; the adapter resolves them per inbound event via the
canonical CredentialVault.resolve_ref entry point.
"""

from __future__ import annotations

import pytest


class _FakeVault:
    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    async def resolve_ref(self, ref, *, org_id, user_id=None):
        self.calls.append((ref, org_id, user_id))
        return self._mapping.get((ref, org_id))


@pytest.mark.asyncio
async def test_resolve_channel_token_slack():
    from surogates.channels.token_resolver import resolve_channel_token

    vault = _FakeVault({
        ("vault://slack_bot_token_A0123ABCD", "o-1"): "xoxb-real",
    })
    token = await resolve_channel_token(
        vault=vault, kind="slack", identifier="A0123ABCD",
        org_id="o-1",
    )
    assert token == "xoxb-real"
    # The call MUST go through resolve_ref 
    assert vault.calls == [
        ("vault://slack_bot_token_A0123ABCD", "o-1", None),
    ]


@pytest.mark.asyncio
async def test_resolve_channel_token_telegram():
    from surogates.channels.token_resolver import resolve_channel_token

    vault = _FakeVault({
        ("vault://telegram_bot_token_@my_bot", "o-1"): "1234:abc",
    })
    token = await resolve_channel_token(
        vault=vault, kind="telegram", identifier="@my_bot",
        org_id="o-1",
    )
    assert token == "1234:abc"


@pytest.mark.asyncio
async def test_resolve_channel_token_missing_returns_none():
    from surogates.channels.token_resolver import resolve_channel_token

    vault = _FakeVault({})
    token = await resolve_channel_token(
        vault=vault, kind="slack", identifier="A0123ABCD",
        org_id="o-1",
    )
    assert token is None


@pytest.mark.asyncio
async def test_resolve_channel_token_arbitrary_kind_returns_none():
    """The framework is open-ended over channel kinds, so an
    as-yet-unknown kind does NOT raise -- with no credential
    configured it simply resolves to None, which the caller treats
    as a 'channel misconfigured' state."""
    from surogates.channels.token_resolver import resolve_channel_token

    vault = _FakeVault({})
    token = await resolve_channel_token(
        vault=vault, kind="discord", identifier="x",
        org_id="o-1",
    )
    assert token is None


def test_vault_ref_for_channel_helper():
    """Pure-function helper used by tests + admin tooling that
    want the canonical vault ref shape without standing up a
    full resolver."""
    from surogates.channels.token_resolver import vault_ref_for_channel

    assert vault_ref_for_channel("slack", "bot_token", "A0123ABCD") == (
        "vault://slack_bot_token_A0123ABCD"
    )
    assert vault_ref_for_channel("telegram", "bot_token", "@my_bot") == (
        "vault://telegram_bot_token_@my_bot"
    )
    # Open-ended over kinds: an as-yet-unknown kind produces the
    # generic ref shape rather than raising.
    assert vault_ref_for_channel("teams", "bot_token", "x") == (
        "vault://teams_bot_token_x"
    )
