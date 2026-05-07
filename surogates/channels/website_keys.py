"""Publishable-key helpers for the website channel.

A publishable key is the bearer token a website embed presents on
``POST /v1/website/sessions`` to bootstrap a visitor session.  It is
safe to ship to the browser **only** when paired with a server-side
``Origin`` allow-list — the key alone is not authority; the
``(key, origin)`` pair is.

The key lives in deploy-time config (``website.publishable_key``).
Rotation is a redeploy: the secret never appears anywhere else, so
revoking a leaked key is just "deploy with a fresh value."  No hashing
is required because we never persist the key — we compare the
incoming bearer token constant-time against the configured value.

The ``surg_wk_`` prefix distinguishes publishable keys from
service-account keys (``surg_sk_``) at the auth boundary so a token of
the wrong shape is rejected before any expensive comparison.
"""

from __future__ import annotations

import hmac
import secrets

__all__ = [
    "PUBLISHABLE_KEY_PREFIX",
    "generate_publishable_key",
    "is_publishable_key",
    "verify_publishable_key",
]


PUBLISHABLE_KEY_PREFIX = "surg_wk_"
# 33 bytes → 44 base64url characters → ~264 bits of entropy.  Enough
# that a random guess collides with a deployed key roughly never; the
# Origin allow-list is the second half of the auth, so the key only
# needs to resist online guessing of one specific deployment's value.
_SECRET_BYTES = 33


def is_publishable_key(token: str) -> bool:
    """Return True when *token* carries the publishable-key prefix.

    Used as a cheap early reject before constant-time compare so a
    misrouted access/refresh JWT or service-account key fails fast
    without leaking timing information about the configured value.
    """
    return token.startswith(PUBLISHABLE_KEY_PREFIX)


def generate_publishable_key() -> str:
    """Return a freshly minted, high-entropy publishable key.

    Convenience for ops scripts and tests.  Production keys are minted
    once during agent provisioning and shipped into the Helm secret —
    the runtime path never calls this.
    """
    return PUBLISHABLE_KEY_PREFIX + secrets.token_urlsafe(_SECRET_BYTES)


def verify_publishable_key(presented: str, expected: str) -> bool:
    """Constant-time compare *presented* against *expected*.

    Returns False when either side is empty (a deployment without a
    configured key cannot authenticate any request, deliberately).
    ``hmac.compare_digest`` avoids leaking the configured value's
    length or shared prefix through timing.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented, expected)
