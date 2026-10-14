"""A fake integrator's webhook and a fake browser push subscription, for laptop tests.

Webhook: set the agent's webhook in Studio (ops) to ``http://127.0.0.1:58140/hook``
and give this the secret ops shows.  Every event is checked against ops' signature
and printed; for ``message.deliver`` (a message for a person reached only through
the app), with ``--api-base`` (ops) and ``--api-key`` (one of the agent's API keys
with ``journeys:write``) it reports the message delivered and read, and with
``--reply`` answers as the person, through ops' public API.

Push: ``GET /push/subscription`` returns a subscription (an endpoint on this server
and freshly made browser keys) to POST to ``/v1/push/subscriptions`` as the
signed-in person; each push it then receives is decrypted and printed.
The runtime only pushes to browser push services, so start it with
``SUROGATES_VAPID_EXTRA_PUSH_HOSTS=127.0.0.1`` to let it reach this stub.

    .venv/bin/python scripts/fake_receivers.py --secret s3cret \\
        --api-base http://127.0.0.1:8888 --api-key surg_sk_… --reply "Da, am luat pastila"

``GET /log`` lists everything received.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time

import httpx
import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
from fastapi import FastAPI, Request, Response

from surogates.channels.web_push import _point, b64u

app = FastAPI()
LOG: list[dict] = []
ARGS = argparse.Namespace(secret="s3cret", api_base="", api_key="", reply="", port=58140)
BROWSER = {"key": ec.generate_private_key(ec.SECP256R1()), "auth": os.urandom(16)}


def signed(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    """Ops' webhook signature: ``v1=<hex HMAC-SHA256(secret, "{timestamp}.{body}")>``, fresh."""
    try:
        fresh = abs(time.time() - int(timestamp)) <= 300
    except ValueError:
        return False
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return fresh and hmac.compare_digest(f"v1={mac}", signature)


@app.post("/hook")
async def hook(request: Request) -> Response:
    body = await request.body()
    ok = signed(ARGS.secret, body, request.headers.get("X-Surogate-Timestamp", ""),
                request.headers.get("X-Surogate-Signature", ""))
    event = json.loads(body) if ok else {}
    data = event.get("data") or {}
    LOG.append({"at": time.strftime("%H:%M:%S"), "webhook": event, "signature_ok": ok})
    print(f"[fake-hook] {'signed' if ok else 'BAD SIGNATURE'} {event.get('type')}: {data.get('kind')} for "
          f"{data.get('external_id')}: {data.get('text')!r}")
    if not ok:
        return Response(status_code=401)
    if event.get("type") == "message.deliver" and ARGS.api_base and ARGS.api_key:
        agent = event["agent_id"]
        async with httpx.AsyncClient(base_url=ARGS.api_base, verify=False,
                                     headers={"Authorization": f"Bearer {ARGS.api_key}"}) as api:
            for status in ("delivered", "read"):
                r = await api.post(f"/api/v1/agents/{agent}/messages/{data['message_id']}/status",
                                   json={"status": status})
                print(f"[fake-hook] reported {status}: {r.status_code}")
            if ARGS.reply:
                r = await api.post(f"/api/v1/agents/{agent}/people/api:{data['external_id']}/messages",
                                   json={"text": ARGS.reply, "in_reply_to": data["message_id"]})
                print(f"[fake-hook] replied as the person: {r.status_code} {r.text[:200]}")
    return Response(status_code=204)


@app.get("/push/subscription")
async def subscription() -> dict:
    return {"endpoint": f"http://127.0.0.1:{ARGS.port}/push/endpoint",
            "keys": {"p256dh": b64u(_point(BROWSER["key"].public_key())), "auth": b64u(BROWSER["auth"])}}


def decrypt(body: bytes) -> bytes:
    """RFC 8291, as a browser reads it."""
    salt, idlen = body[:16], body[20]
    as_public, sealed = body[21:21 + idlen], body[21 + idlen:]
    ua = BROWSER["key"]
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = HKDF(hashes.SHA256(), 32, BROWSER["auth"],
               b"WebPush: info\0" + _point(ua.public_key()) + as_public).derive(shared)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\0").derive(prk)
    nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\0").derive(prk)
    return AESGCM(cek).decrypt(nonce, sealed, None).rstrip(b"\x00")[:-1]


@app.post("/push/endpoint")
async def push(request: Request) -> Response:
    payload = json.loads(decrypt(await request.body()))
    LOG.append({"at": time.strftime("%H:%M:%S"), "push": payload,
                "vapid": request.headers.get("Authorization", "")[:40]})
    print(f"[fake-push] {payload.get('title')}: {payload.get('body')!r}")
    return Response(status_code=201)


@app.get("/log")
async def log() -> list[dict]:
    return LOG


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=58140)
    parser.add_argument("--secret", default="s3cret", help="the agent's webhook_secret")
    parser.add_argument("--api-base", default="", help="the ops server, to report back to")
    parser.add_argument("--api-key", default="", help="one of the agent's API keys")
    parser.add_argument("--reply", default="", help="answer every message with this, as the person")
    ARGS = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=ARGS.port, log_level="warning")
