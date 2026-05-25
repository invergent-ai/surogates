"""Firebase ID token verification.

Validates Firebase ID tokens against the configured Firebase project
using Google's public x509 certificates. The certificate set is cached
for the lifetime indicated by Google's Cache-Control header so the
auth path doesn't hit the network on every login.

The verification pipeline mirrors what the Firebase Admin SDK does:

* Header MUST use RS256.
* ``kid`` MUST match one of the published certs.
* ``aud`` MUST equal the configured Firebase project id.
* ``iss`` MUST equal ``https://securetoken.google.com/{project_id}``.
* ``exp`` / ``iat`` / ``auth_time`` MUST be present and reasonable.
* ``sub`` (the Firebase UID) MUST be a non-empty string.

A ``FirebaseTokenError`` is raised on any verification failure so
callers can surface a uniform 401 / 400 response.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import jwt
from cryptography import x509


FIREBASE_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/"
    "securetoken@system.gserviceaccount.com"
)


class FirebaseTokenError(ValueError):
    """Raised on any verification failure (bad signature, audience, etc.)."""


@dataclass
class _CertCache:
    certificates: dict[str, str]
    expires_at: float


_cert_cache = _CertCache(certificates={}, expires_at=0.0)


def firebase_auth_provider_name(firebase_project_id: str) -> str:
    """Compose the namespaced ``auth_provider`` for a BYO Firebase project.

    Firebase UIDs are only unique within a single Firebase project; two
    different BYO projects can mint the same UID for different real
    users. Namespacing the ``auth_provider`` column with the Firebase
    project id prevents cross-project collisions when looking up users
    by ``(auth_provider, external_id)``.
    """
    project_id = firebase_project_id.strip()
    if not project_id:
        raise ValueError("Firebase project id is required")
    return f"firebase:{project_id}"


async def verify_firebase_id_token(
    id_token: str, project_id: str,
) -> dict[str, Any]:
    """Verify a Firebase ID token against ``project_id``.

    Returns the decoded claims dict on success. Raises
    :class:`FirebaseTokenError` on any failure.
    """
    if not project_id:
        raise FirebaseTokenError("Firebase project id is not configured")

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError as exc:
        raise FirebaseTokenError("Invalid Firebase token header") from exc

    if header.get("alg") != "RS256":
        raise FirebaseTokenError("Firebase token must use RS256")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise FirebaseTokenError("Firebase token is missing a key id")

    certificate = await _resolve_certificate(kid)
    if certificate is None:
        raise FirebaseTokenError("Firebase token key id is not trusted")

    try:
        payload = jwt.decode(
            id_token,
            _public_key_from_certificate(certificate),
            algorithms=["RS256"],
            audience=project_id,
            issuer=f"https://securetoken.google.com/{project_id}",
            options={
                "require": ["exp", "iat", "aud", "iss", "sub", "auth_time"],
            },
        )
    except jwt.PyJWTError as exc:
        raise FirebaseTokenError("Invalid Firebase token") from exc

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise FirebaseTokenError("Firebase token subject is missing")

    auth_time = payload.get("auth_time")
    if not isinstance(auth_time, int):
        raise FirebaseTokenError("Firebase token auth_time is invalid")
    if auth_time > int(datetime.now(timezone.utc).timestamp()):
        raise FirebaseTokenError("Firebase token auth_time is in the future")

    return payload


async def _resolve_certificate(kid: str) -> str | None:
    """Look up the certificate for ``kid``, refreshing the cache if missing.

    Firebase rotates signing keys; if our cached set doesn't contain
    ``kid`` we evict the cache and re-fetch once before giving up. This
    keeps a single cold-key login fast in the steady state while still
    tolerating Google's key rollovers without a service restart.
    """
    certificates = await _get_firebase_certificates()
    certificate = certificates.get(kid)
    if certificate is None:
        _cert_cache.certificates = {}
        _cert_cache.expires_at = 0.0
        certificates = await _get_firebase_certificates()
        certificate = certificates.get(kid)
    return certificate


def _public_key_from_certificate(certificate: str) -> Any:
    try:
        parsed = x509.load_pem_x509_certificate(certificate.encode("utf-8"))
    except ValueError as exc:
        raise FirebaseTokenError("Firebase certificate is invalid") from exc
    return parsed.public_key()


async def _get_firebase_certificates() -> dict[str, str]:
    now = time.time()
    if _cert_cache.certificates and _cert_cache.expires_at > now:
        return _cert_cache.certificates
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(FIREBASE_CERTS_URL)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise FirebaseTokenError("Firebase cert response is invalid")
    _cert_cache.certificates = {str(k): str(v) for k, v in payload.items()}
    _cert_cache.expires_at = _certificate_expiry(response.headers)
    return _cert_cache.certificates


def _certificate_expiry(headers: httpx.Headers) -> float:
    """Compute the certificate cache TTL from Google's response headers."""
    cache_control = headers.get("cache-control", "")
    for directive in cache_control.split(","):
        name, _, value = directive.strip().partition("=")
        if name.lower() == "max-age" and value.isdigit():
            return time.time() + int(value)
    expires = headers.get("expires")
    if expires:
        try:
            return parsedate_to_datetime(expires).timestamp()
        except (TypeError, ValueError, IndexError):
            pass
    # Fall back to one hour — long enough to amortise the network round
    # trip but short enough that key rotation doesn't break logins for
    # long.
    return time.time() + 3600
