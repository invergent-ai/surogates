"""Per-event credential resolution for shared channel adapters.

The shared adapter pod serves many tenants;
credentials for an inbound event live in the per-tenant
credential vault, not in process-wide settings.  Resolution
goes through :meth:`CredentialVault.resolve_ref`

Vault key conventions (per channel kind and credential name):

* ``slack``    -> ``vault://slack_<cred>_<api_app_id>``
* ``telegram`` -> ``vault://telegram_<cred>_<bot_username>``

Examples::

    vault://slack_bot_token_A0123ABCD
    vault://slack_signing_secret_A0123ABCD
    vault://telegram_bot_token_@my_bot
    vault://telegram_webhook_secret_@my_bot

The ref format is generic over channel kind: adding a platform is a
code-only change, so :func:`vault_ref_for_channel` deliberately does
NOT enumerate kinds.  A wrong/misspelled kind simply resolves to a
missing vault entry (``None``), which the caller treats as 'channel
misconfigured'.  The website widget channel does not need a per-tenant
token (its inbound flow is HTTP-authenticated against the api itself).
"""

from __future__ import annotations

from typing import Any

__all__ = ["resolve_channel_token", "vault_ref_for_channel"]


def vault_ref_for_channel(kind: str, cred: str, identifier: str) -> str:
    """Build the canonical ``vault://`` reference for a channel credential.

    Parameters
    ----------
    kind:
        Channel kind, e.g. ``"slack"`` or ``"telegram"``.  Any
        non-empty string is accepted -- the framework is open-ended
        over channel kinds (adding a platform is a code-only change).
    cred:
        Credential name, e.g. ``"bot_token"`` or ``"signing_secret"``.
    identifier:
        Per-tenant identifier, e.g. Slack API app-id or Telegram
        bot username.

    Returns a string of the form ``vault://<kind>_<cred>_<identifier>``.
    Pure function; useful from admin tooling that wants the ref shape
    without a vault instance.
    """
    return f"vault://{kind}_{cred}_{identifier}"


async def resolve_channel_token(
    *,
    vault: Any,
    kind: str,
    identifier: str,
    org_id: str,
) -> str | None:
    """Resolve the per-tenant bot token via CredentialVault.

    Thin wrapper around :func:`vault_ref_for_channel` for the
    ``bot_token`` credential.  Called by the platform webhook
    dispatchers (``surogates.channels.platforms.slack``,
    ``surogates.channels.platforms.telegram``, …) during inbound
    event routing.

    Returns the decrypted token, or ``None`` if no credential is
    configured for this (kind, identifier, org_id) -- the caller
    surfaces this as a structured 'channel misconfigured' error
    and drops the inbound event rather than crashing the adapter.
    """
    ref = vault_ref_for_channel(kind, "bot_token", identifier)
    return await vault.resolve_ref(ref, org_id=org_id)
