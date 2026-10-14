"""Browser push: the payload encrypted exactly as RFC 8291 says, the request signed as RFC 8292 says."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt

from surogates.channels import web_push
from surogates.channels.web_push import b64u, encrypt, generate_vapid_keys, unb64u, vapid_header

# RFC 8291 Appendix A
PLAINTEXT = b"When I grow up, I want to be a watermelon"
AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
BODY = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru"
        "3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_the_payload_is_encrypted_exactly_as_rfc_8291_appendix_a():
    as_key = ec.derive_private_key(int.from_bytes(unb64u(AS_PRIVATE), "big"), ec.SECP256R1())
    body = encrypt(PLAINTEXT, unb64u(UA_PUBLIC), unb64u(AUTH), salt=unb64u(SALT), as_key=as_key)
    assert b64u(body) == BODY


def test_the_request_is_signed_for_the_push_services_origin():
    public, private = generate_vapid_keys()
    header = vapid_header("https://fcm.googleapis.com/fcm/send/abc", public, private, "mailto:ops@example.com")
    token = header.split("t=", 1)[1].split(",", 1)[0]
    assert header.endswith(f"k={public}")
    pem_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(public))
    from cryptography.hazmat.primitives import serialization

    pem = pem_public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    claims = jwt.decode(token, pem.decode(), algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:ops@example.com"


class Http:
    def __init__(self, statuses):
        self.statuses, self.sent = list(statuses), []

    async def post(self, url, *, content, headers, timeout):
        self.sent.append((url, headers))
        return SimpleNamespace(status_code=self.statuses.pop(0))


async def test_a_person_is_pushed_on_each_browser_and_a_gone_one_is_forgotten(sf, monkeypatch):
    import sqlalchemy as sa

    from surogates.db.models import Base, WebPushSubscription

    async with sf() as db:
        await db.run_sync(lambda s: Base.metadata.tables["web_push_subscriptions"].create(s.connection()))
    user, ua = uuid4(), ec.generate_private_key(ec.SECP256R1())
    ua_public = b64u(web_push._point(ua.public_key()))
    async with sf() as db:
        for host in ("fcm.googleapis.com", "updates.push.services.mozilla.com"):
            db.add(WebPushSubscription(org_id=uuid4(), agent_id="agent-1", user_id=user,
                                       endpoint=f"https://{host}/x", p256dh=ua_public, auth=b64u(b"0" * 16)))
        await db.commit()
    public, private = generate_vapid_keys()
    settings = SimpleNamespace(public_key=public, private_key=private, subject="mailto:a@b")
    session = SimpleNamespace(id=uuid4(), user_id=user, agent_id="agent-1", org_id=uuid4())
    assert await web_push.push_to_person(sf, Http([]), settings, session, "x") == 0  # the agent has push off
    from surogates.followup.store import update_settings

    await update_settings(sf, org_id=session.org_id, agent_id="agent-1", web_push=True)
    http = Http([201, 410])
    assert await web_push.push_to_person(sf, http, settings, session, "Ai uitat pastila?") == 1
    assert {h["Content-Encoding"] for _, h in http.sent} == {"aes128gcm"}
    async with sf() as db:
        left = (await db.execute(sa.select(WebPushSubscription.endpoint))).scalars().all()
    assert left == ["https://fcm.googleapis.com/x"]
    # no keys: nothing is sent
    assert await web_push.push_to_person(sf, Http([]), SimpleNamespace(public_key="", private_key="",
                                                                       subject=""), session, "x") == 0


async def test_a_subscription_can_only_point_at_a_browser_push_service(monkeypatch):
    import asyncio
    import socket

    async def resolve(self, host, port, **kw):
        address = {"fcm.googleapis.com": "142.250.1.1", "evil.push.apple.com": "10.0.0.5"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    assert await web_push.endpoint_problem("https://fcm.googleapis.com/fcm/send/x") is None
    for bad in ("https://metadata.google.internal/x", "http://fcm.googleapis.com/x",
                "https://fcm.googleapis.com.evil.io/x", "https://10.0.0.1/x"):
        assert await web_push.endpoint_problem(bad), bad
    assert "private" in await web_push.endpoint_problem("https://evil.push.apple.com/x")
    # A laptop's stub only when configured, and never resolved.
    assert await web_push.endpoint_problem("http://127.0.0.1:8099/push", "127.0.0.1") is None


def test_keys_must_be_a_browser_subscriptions():
    ua = ec.generate_private_key(ec.SECP256R1())
    point = b64u(web_push._point(ua.public_key()))
    assert web_push.keys_problem(point, b64u(b"0" * 16)) is None
    assert web_push.keys_problem("not base64!!", b64u(b"0" * 16))
    assert web_push.keys_problem(point, b64u(b"0" * 8))


async def test_a_stored_endpoint_off_the_push_services_is_never_posted_to():
    http = Http([])
    sub = SimpleNamespace(endpoint="https://169.254.169.254/latest", p256dh="", auth="")
    assert await web_push.send(http, SimpleNamespace(), sub, {}) == 410
    assert http.sent == []
