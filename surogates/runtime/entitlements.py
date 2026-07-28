# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
"""Per-session purchased-package reader.

The commerce/allowance gates pin the end-user's effective feature
package on ``session.config["entitlements"]`` from the ops authorize
receipt (see ``surogates.api.routes._commerce_turn.pin_entitlements``).
This module is the single reader: the worker's tool gating, the
harness's tool filter and slash gate, and the KB tools all interpret
the pinned dict through these helpers so the semantics cannot drift.

Shape: ``{capabilities?, channels?, kb_ids?, mcp_server_ids?}`` where an
absent key means the dimension is unrestricted and a list (possibly
empty) is an exact allowlist. No pin at all means no restriction: the
agent's own configuration shapes the session, exactly as before
packages existed.
"""

from __future__ import annotations

ENTITLEMENTS_CONFIG_KEY = "entitlements"

#: Capability ids sellable in a package (mirrors ops
#: ``core.commerce.features.CAPABILITY_VOCAB``). Slash-command ids are
#: hyphenated on the wire (``deep-research``); package capabilities use
#: underscores — :func:`capability_allowed` normalizes.
_SLASH_CAPABILITIES = frozenset({
    "compress", "code", "deep_research", "auto_research",
    "loop", "mission", "goal",
})


def pinned_entitlements(session_config: dict | None) -> dict | None:
    """The pinned package dict, or ``None`` when unrestricted."""
    value = (session_config or {}).get(ENTITLEMENTS_CONFIG_KEY)
    return value if isinstance(value, dict) else None


def dimension_allowlist(
    session_config: dict | None, dimension: str,
) -> frozenset[str] | None:
    """The allowlist for one dimension; ``None`` = unrestricted."""
    entitlements = pinned_entitlements(session_config)
    if entitlements is None:
        return None
    values = entitlements.get(dimension)
    if values is None:
        return None
    return frozenset(str(v) for v in values)


def capability_allowed(
    session_config: dict | None, capability: str,
) -> bool:
    """Whether the package includes ``capability``.

    Accepts both the package spelling (``deep_research``) and the
    slash-command spelling (``deep-research``). Capabilities outside
    the sellable vocabulary are never restricted, so unrelated slash
    commands (``clear``, skill invocations) pass untouched.
    """
    normalized = capability.replace("-", "_")
    allowed = dimension_allowlist(session_config, "capabilities")
    if allowed is None:
        return True
    if normalized not in _SLASH_CAPABILITIES | {"browser", "brainstorming"}:
        return True
    return normalized in allowed


def kb_allowed(session_config: dict | None, kb_id: str) -> bool:
    """Whether the package includes knowledge base ``kb_id``."""
    allowed = dimension_allowlist(session_config, "kb_ids")
    return allowed is None or str(kb_id) in allowed


__all__ = [
    "ENTITLEMENTS_CONFIG_KEY",
    "capability_allowed",
    "dimension_allowlist",
    "kb_allowed",
    "pinned_entitlements",
]
