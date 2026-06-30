"""Multi-credential resolution for shared channel adapters.

Platforms like Slack require more than one secret per tenant
(e.g. ``bot_token`` *and* ``signing_secret``).  This module
provides :func:`resolve_channel_credentials`, which resolves a
named set of credentials from the vault in a single call and
returns ``None`` for any secret that is absent — the caller
decides whether to surface a structured 'channel misconfigured'
error or drop the event.

See :mod:`surogates.channels.token_resolver` for the single-
credential ``resolve_channel_token`` wrapper used by existing
channel adapters.
"""

from __future__ import annotations

from typing import Any

from surogates.channels.token_resolver import vault_ref_for_channel

__all__ = ["resolve_channel_credentials"]


async def resolve_channel_credentials(
    *,
    vault: Any,
    kind: str,
    identifier: str,
    org_id: str,
    refs: dict[str, str],
) -> dict[str, str | None]:
    """Resolve multiple per-tenant credentials from the vault.

    Parameters
    ----------
    vault:
        A :class:`CredentialVault` instance (or compatible object
        with an async ``resolve_ref(ref, *, org_id)`` method).
    kind:
        Channel kind, e.g. ``"slack"`` or ``"telegram"``.
    identifier:
        Per-tenant identifier, e.g. Slack API app-id or Telegram
        bot username.
    org_id:
        Organisation ID passed to ``vault.resolve_ref``.
    refs:
        Mapping of *logical name* → *credential name*, e.g.::

            {"bot_token": "bot_token", "signing_secret": "signing_secret"}

        Each entry produces one vault lookup using
        :func:`~surogates.channels.token_resolver.vault_ref_for_channel`.

    Returns
    -------
    dict[str, str | None]
        A dict with the same keys as *refs*, each mapped to the
        resolved secret string or ``None`` if that secret is not
        present in the vault.  Never raises on a missing secret; a
        wrong/misspelled *kind* simply resolves every ref to ``None``.
    """
    result: dict[str, str | None] = {}
    for logical_name, cred_name in refs.items():
        ref = vault_ref_for_channel(kind, cred_name, identifier)
        value = await vault.resolve_ref(ref, org_id=org_id)
        result[logical_name] = value
    return result
