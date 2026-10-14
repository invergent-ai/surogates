"""Browser push (Web Push with VAPID) to the people who allowed it.

A person's browser subscribes once per agent (``POST /v1/push/subscriptions``);
:func:`push_to_person` sends the agent's message to each of their browsers
when it lands in their web chat while they are not looking.  The payload is
encrypted for the browser (RFC 8291, ``aes128gcm``) and the request signed
with the platform's VAPID key (RFC 8292), so no push service can read it.

Keys: ``SUROGATES_VAPID_PUBLIC_KEY`` / ``_PRIVATE_KEY`` are the
uncompressed P-256 public point and the raw private scalar, base64url, as
``npx web-push generate-vapid-keys`` prints them; :func:`generate_vapid_keys`
makes a pair.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import sqlalchemy as sa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
from sqlalchemy.dialects.postgresql import insert as pg_insert

from surogates.db.models import WebPushSubscription

logger = logging.getLogger(__name__)

RECORD_SIZE = 4096
TTL_SECONDS = 24 * 3600
#: The push service says this browser is gone for good: forget it.
GONE = (404, 410)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def generate_vapid_keys() -> tuple[str, str]:
    """``(public, private)``, base64url, for ``SUROGATES_VAPID_*``."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64u(_point(key.public_key())), b64u(key.private_numbers().private_value.to_bytes(32, "big"))


def encrypt(plaintext: bytes, ua_public: bytes, auth_secret: bytes, *,
            salt: bytes | None = None, as_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """The ``aes128gcm`` body for one browser (RFC 8291 §3.4, one record)."""
    salt = salt or os.urandom(16)
    as_key = as_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _point(as_key.public_key())
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\0" + ua_public + as_public).derive(shared)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()  # HKDF-Extract
    cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\0").derive(prk)
    nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\0").derive(prk)
    sealed = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + sealed


def vapid_header(endpoint: str, public_key: str, private_key: str, subject: str) -> str:
    """``Authorization`` for one push service: a 12-hour ES256 JWT for its origin."""
    from jose import jwt

    parts = urlsplit(endpoint)
    d = int.from_bytes(unb64u(private_key), "big")
    pem = ec.derive_private_key(d, ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    token = jwt.encode({"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600,
                        "sub": subject}, pem.decode(), algorithm="ES256")
    return f"vapid t={token}, k={public_key}"


async def send(http: Any, settings: Any, sub: Any, payload: dict) -> int:
    """Push *payload* to one subscription; returns the push service's status."""
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode(), unb64u(sub.p256dh), unb64u(sub.auth))
    resp = await http.post(sub.endpoint, content=body, timeout=10.0, headers={
        "Authorization": vapid_header(sub.endpoint, settings.public_key, settings.private_key, settings.subject),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(TTL_SECONDS), "Urgency": "high",
    })
    return resp.status_code


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


async def subscribe(session_factory: Any, *, org_id: uuid.UUID, agent_id: str, user_id: uuid.UUID,
                    endpoint: str, p256dh: str, auth: str) -> None:
    """Remember a browser, and that its person may be written to first on web.

    Allowing notifications is the person's own opt-in, so permission is
    recorded as theirs (``method="push"``) on their ``web`` identity, which
    is created here if they never had one.
    """
    from surogates.db.models import ChannelIdentity
    from surogates.followup.store import grant_contact_permission

    now = datetime.now(timezone.utc)
    async with session_factory() as db:
        await db.execute(
            pg_insert(WebPushSubscription)
            .values(id=uuid.uuid4(), org_id=org_id, agent_id=agent_id, user_id=user_id,
                    endpoint=endpoint, p256dh=p256dh, auth=auth, created_at=now)
            .on_conflict_do_update(
                index_elements=["endpoint"],
                set_={"org_id": org_id, "agent_id": agent_id, "user_id": user_id,
                      "p256dh": p256dh, "auth": auth, "created_at": now},
            )
        )
        await db.execute(
            pg_insert(ChannelIdentity)
            .values(org_id=org_id, user_id=user_id, platform="web", platform_user_id=str(user_id))
            .on_conflict_do_nothing(index_elements=["org_id", "platform", "platform_user_id"])
        )
        await db.commit()
    await grant_contact_permission(session_factory, org_id=org_id, platform="web",
                                   platform_user_id=str(user_id), recorded_by=f"user:{user_id}", method="push")


async def unsubscribe(session_factory: Any, *, user_id: uuid.UUID, endpoint: str) -> bool:
    async with session_factory() as db:
        result = await db.execute(
            sa.delete(WebPushSubscription)
            .where(WebPushSubscription.endpoint == endpoint)
            .where(WebPushSubscription.user_id == user_id)
        )
        await db.commit()
    return bool(result.rowcount)


async def push_to_person(session_factory: Any, http: Any, settings: Any, session: Any, text: str) -> int:
    """Push *text* to every browser the session's person allowed for its agent.

    Returns how many push services took it.  A browser the push service
    reports gone is forgotten.  Nothing is sent without VAPID keys, or when
    the agent has push off (``followup_settings.web_push``).
    """
    from surogates.db.models import FollowupSettingsRow

    if not (settings.public_key and settings.private_key) or session.user_id is None:
        return 0
    async with session_factory() as db:
        agent = await db.get(FollowupSettingsRow, (session.org_id, session.agent_id))
        if agent is None or not agent.web_push:
            return 0
        subs = (await db.execute(
            sa.select(WebPushSubscription)
            .where(WebPushSubscription.user_id == session.user_id)
            .where(WebPushSubscription.agent_id == session.agent_id)
        )).scalars().all()
    payload = {"title": "New message", "body": text[:240],
               "session_id": str(session.id), "agent_id": session.agent_id, "url": f"/chat/{session.id}"}
    sent, gone = 0, []
    for sub in subs:
        try:
            status = await send(http, settings, sub, payload)
        except Exception:  # noqa: BLE001 — one browser, not the person's others
            logger.warning("[push] %s unreachable", urlsplit(sub.endpoint).netloc, exc_info=True)
            continue
        if status in GONE:
            gone.append(sub.id)
        elif 200 <= status < 300:
            sent += 1
        else:
            logger.warning("[push] %s answered %s", urlsplit(sub.endpoint).netloc, status)
    if gone:
        async with session_factory() as db:
            await db.execute(sa.delete(WebPushSubscription).where(WebPushSubscription.id.in_(gone)))
            await db.commit()
    return sent
