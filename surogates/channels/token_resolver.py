"""Per-event bot token resolution for shared channel adapters.

The shared adapter pod serves many tenants;
the bot token for an inbound event lives in the per-tenant
credential vault, not in process-wide settings.  Resolution
goes through :meth:`CredentialVault.resolve_ref`

Vault key conventions (per channel kind):

* ``slack``    -> ``vault://slack_bot_token_<api_app_id>``
* ``telegram`` -> ``vault://telegram_bot_token_<bot_username>``

The website widget channel does not need a per-tenant token
(its inbound flow is HTTP-authenticated against the api itself),
so it is intentionally NOT in :data:`_KIND_TO_PREFIX`.
"""

from __future__ import annotations

from typing import Any

__all__ = ["resolve_channel_token", "vault_ref_for_channel"]


_KIND_TO_PREFIX = {
    "slack": "vault://slack_bot_token_",
    "telegram": "vault://telegram_bot_token_",
}


def vault_ref_for_channel(kind: str, identifier: str) -> str:
    """Build the canonical ``vault://`` reference for a channel
    bot token.  Pure function; useful from admin tooling that
    wants the ref shape without a vault instance."""
    prefix = _KIND_TO_PREFIX.get(kind)
    if prefix is None:
        raise ValueError(
            f"Unknown channel kind for vault ref: {kind!r}"
        )
    return f"{prefix}{identifier}"


async def resolve_channel_token(
    *,
    vault: Any,
    kind: str,
    identifier: str,
    org_id: str,
) -> str | None:
    """Resolve the per-tenant bot token via CredentialVault.

    Returns the decrypted token, or ``None`` if no credential is
    configured for this (kind, identifier, org_id) -- the caller
    surfaces this as a structured 'channel misconfigured' error
    and drops the inbound event rather than crashing the adapter.
    """
    ref = vault_ref_for_channel(kind, identifier)
    return await vault.resolve_ref(ref, org_id=org_id)
