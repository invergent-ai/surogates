"""Tests for resolve_channel_credentials and the generalised
vault_ref_for_channel(kind, cred, identifier) helper.

resolve_channel_credentials returns a dict mapping each logical
name to its resolved secret (or None if absent).  It does NOT
raise on a missing secret; the caller decides whether to surface a
structured 'channel misconfigured' error or drop the event.
"""

from __future__ import annotations

import pytest


class _FakeVault:
    def __init__(self, mapping):
        self._mapping = mapping
        self.calls = []

    async def resolve_ref(self, ref, *, org_id, user_id=None):
        self.calls.append((ref, org_id))
        return self._mapping.get((ref, org_id))


# ---------------------------------------------------------------------------
# vault_ref_for_channel — 3-arg generalisation
# ---------------------------------------------------------------------------


def test_vault_ref_for_channel_bot_token():
    """Backwards-compatible: cred='bot_token' produces the same
    vault ref as the old 2-arg version did."""
    from surogates.channels.token_resolver import vault_ref_for_channel

    assert vault_ref_for_channel("slack", "bot_token", "A0123ABCD") == (
        "vault://slack_bot_token_A0123ABCD"
    )
    assert vault_ref_for_channel("telegram", "bot_token", "@my_bot") == (
        "vault://telegram_bot_token_@my_bot"
    )


def test_vault_ref_for_channel_signing_secret():
    """New cred names produce distinct vault refs."""
    from surogates.channels.token_resolver import vault_ref_for_channel

    assert vault_ref_for_channel("slack", "signing_secret", "A0123ABCD") == (
        "vault://slack_signing_secret_A0123ABCD"
    )


def test_vault_ref_for_channel_telegram_webhook_secret():
    from surogates.channels.token_resolver import vault_ref_for_channel

    assert vault_ref_for_channel("telegram", "webhook_secret", "@my_bot") == (
        "vault://telegram_webhook_secret_@my_bot"
    )


def test_vault_ref_for_channel_arbitrary_kind():
    """The framework is open-ended over channel kinds (adding a
    platform is a code-only change), so an as-yet-unknown kind must
    NOT raise -- it just produces the generic ref shape."""
    from surogates.channels.token_resolver import vault_ref_for_channel

    assert vault_ref_for_channel("teams", "app_secret", "T9") == (
        "vault://teams_app_secret_T9"
    )
    assert vault_ref_for_channel("whatsapp", "bot_token", "W1") == (
        "vault://whatsapp_bot_token_W1"
    )


# ---------------------------------------------------------------------------
# resolve_channel_credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_channel_credentials_all_present():
    """All requested secrets present → dict with all values filled in."""
    from surogates.channels.credentials import resolve_channel_credentials

    vault = _FakeVault({
        ("vault://slack_bot_token_A0123ABCD", "o-1"): "xoxb-real",
        ("vault://slack_signing_secret_A0123ABCD", "o-1"): "sig-secret",
    })
    result = await resolve_channel_credentials(
        vault=vault,
        kind="slack",
        identifier="A0123ABCD",
        org_id="o-1",
        refs={"bot_token": "bot_token", "signing_secret": "signing_secret"},
    )
    assert result == {
        "bot_token": "xoxb-real",
        "signing_secret": "sig-secret",
    }


@pytest.mark.asyncio
async def test_resolve_channel_credentials_partial_missing():
    """A secret absent from the vault → None for that key; no exception."""
    from surogates.channels.credentials import resolve_channel_credentials

    vault = _FakeVault({
        ("vault://slack_bot_token_A0123ABCD", "o-1"): "xoxb-real",
        # signing_secret deliberately absent
    })
    result = await resolve_channel_credentials(
        vault=vault,
        kind="slack",
        identifier="A0123ABCD",
        org_id="o-1",
        refs={"bot_token": "bot_token", "signing_secret": "signing_secret"},
    )
    assert result == {
        "bot_token": "xoxb-real",
        "signing_secret": None,
    }


@pytest.mark.asyncio
async def test_resolve_channel_credentials_all_missing():
    """All secrets absent → all None; no exception raised."""
    from surogates.channels.credentials import resolve_channel_credentials

    vault = _FakeVault({})
    result = await resolve_channel_credentials(
        vault=vault,
        kind="slack",
        identifier="A0123ABCD",
        org_id="o-1",
        refs={"bot_token": "bot_token", "signing_secret": "signing_secret"},
    )
    assert result == {
        "bot_token": None,
        "signing_secret": None,
    }


@pytest.mark.asyncio
async def test_resolve_channel_credentials_single_ref():
    """Works with a single logical name (minimal case)."""
    from surogates.channels.credentials import resolve_channel_credentials

    vault = _FakeVault({
        ("vault://telegram_bot_token_@my_bot", "o-2"): "1234:abc",
    })
    result = await resolve_channel_credentials(
        vault=vault,
        kind="telegram",
        identifier="@my_bot",
        org_id="o-2",
        refs={"bot_token": "bot_token"},
    )
    assert result == {"bot_token": "1234:abc"}


@pytest.mark.asyncio
async def test_resolve_channel_credentials_vault_calls():
    """Each logical ref drives exactly one vault.resolve_ref call."""
    from surogates.channels.credentials import resolve_channel_credentials

    vault = _FakeVault({})
    await resolve_channel_credentials(
        vault=vault,
        kind="slack",
        identifier="APP1",
        org_id="o-3",
        refs={"bot_token": "bot_token", "signing_secret": "signing_secret"},
    )
    # The vault must have been consulted for each ref exactly once.
    assert set(vault.calls) == {
        ("vault://slack_bot_token_APP1", "o-3"),
        ("vault://slack_signing_secret_APP1", "o-3"),
    }
    assert len(vault.calls) == 2


@pytest.mark.asyncio
async def test_resolve_channel_credentials_arbitrary_kind():
    """An as-yet-unknown channel kind does NOT raise; absent secrets
    just resolve to None (caller treats it as 'channel misconfigured')."""
    from surogates.channels.credentials import resolve_channel_credentials

    vault = _FakeVault({
        ("vault://teams_app_secret_T9", "o-1"): "teams-secret",
    })
    result = await resolve_channel_credentials(
        vault=vault,
        kind="teams",
        identifier="T9",
        org_id="o-1",
        refs={"app_secret": "app_secret", "bot_token": "bot_token"},
    )
    assert result == {"app_secret": "teams-secret", "bot_token": None}
