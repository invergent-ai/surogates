"""Tests for the WhatsApp Business Cloud API channel platform.

Written BEFORE the implementation module exists (TDD).  Mirrors
tests/test_telegram_platform.py in structure.
"""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest

from surogates.channels.platforms.whatsapp import (
    WhatsAppPlatform,
    identifier_of,
    parse,
    verify,
)
from surogates.channels.registry import VerificationResult

PNID = "7794189252778687"
APP_SECRET = "0123456789abcdef0123456789abcdef"
VERIFY_TOKEN = "a7Fk2verify"
ACCESS_TOKEN = "EAAtoken"
WA_ID = "13557825698"


def _creds(**overrides) -> dict:
    """Credential dict as the dispatcher resolves it from the vault."""
    creds = {
        "access_token": ACCESS_TOKEN,
        "app_secret": APP_SECRET,
        "verify_token": VERIFY_TOKEN,
    }
    creds.update(overrides)
    return creds


def _sign(secret: str, body: bytes) -> str:
    """Recompute the X-Hub-Signature-256 header; never hardcode it."""
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()


def _post_request(raw: bytes, *, secret: str = APP_SECRET, pnid: str = PNID):
    """A signed POST request double (only .headers/.path_params are read)."""
    return SimpleNamespace(
        method="POST",
        path_params={"phone_number_id": pnid},
        headers={"X-Hub-Signature-256": _sign(secret, raw)},
        query_params={},
    )


def _get_request(**query):
    """A GET handshake request double."""
    return SimpleNamespace(
        method="GET",
        path_params={"phone_number_id": PNID},
        headers={},
        query_params=query,
    )


def _text_message(**overrides) -> dict:
    """The canonical inbound text envelope, modelled on Meta's sample."""
    message = {
        "from": WA_ID,
        "id": "wamid.HBgLMTM1NTc4MjU2OTgVAGHAYWYET688aASGNTI1QzZFQjhEMDk2QQA=",
        "timestamp": "1758254144",
        "text": {"body": "Hi!"},
        "type": "text",
    }
    message.update(overrides)
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "215589313241560883",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "15551797781",
                        "phone_number_id": PNID,
                    },
                    "contacts": [
                        {"profile": {"name": "Jessica Laverdetman"}, "wa_id": WA_ID},
                    ],
                    "messages": [message],
                },
            }],
        }],
    }


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


class TestIdentifierOf:
    def test_reads_phone_number_id_from_path(self):
        assert identifier_of(_post_request(b"{}"), None) == PNID

    def test_ignores_body(self):
        # The dispatcher calls this with body=None before parsing.
        assert identifier_of(_post_request(b"{}"), None) == PNID


# ---------------------------------------------------------------------------
# verify — GET handshake
# ---------------------------------------------------------------------------


class TestVerifyHandshake:
    def test_echoes_challenge_as_plain_string(self):
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        })
        result = verify(request, b"", creds=_creds())
        assert isinstance(result, VerificationResult)
        assert result.accepted is True
        assert result.response_body == "1158201444"
        assert result.status_code == 200

    def test_rejects_wrong_token(self):
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "123",
        })
        result = verify(request, b"", creds=_creds())
        assert result.accepted is False

    def test_rejects_wrong_mode(self):
        request = _get_request(**{
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "123",
        })
        assert verify(request, b"", creds=_creds()).accepted is False

    def test_rejects_missing_challenge(self):
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
        })
        assert verify(request, b"", creds=_creds()).accepted is False

    def test_rejects_when_verify_token_unconfigured(self):
        # An unset secret makes compare_digest("", "") true, so an attacker who
        # guesses the misconfiguration could subscribe their own webhook.
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": "",
            "hub.challenge": "123",
        })
        assert verify(request, b"", creds=_creds(verify_token="")).accepted is False

    def test_non_ascii_token_does_not_raise(self):
        # compare_digest on str raises TypeError on non-ASCII; compare bytes.
        request = _get_request(**{
            "hub.mode": "subscribe",
            "hub.verify_token": "tökén",
            "hub.challenge": "123",
        })
        assert verify(request, b"", creds=_creds()).accepted is False


# ---------------------------------------------------------------------------
# verify — POST signature
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_accepts_valid_signature(self):
        raw = b'{"object":"whatsapp_business_account"}'
        assert verify(_post_request(raw), raw, creds=_creds()) is True

    def test_rejects_wrong_secret(self):
        raw = b'{"object":"whatsapp_business_account"}'
        request = _post_request(raw, secret="f" * 32)
        assert verify(request, raw, creds=_creds()) is False

    def test_rejects_missing_header(self):
        raw = b"{}"
        request = SimpleNamespace(
            method="POST", path_params={"phone_number_id": PNID},
            headers={}, query_params={},
        )
        assert verify(request, raw, creds=_creds()) is False

    def test_rejects_header_without_sha256_prefix(self):
        raw = b"{}"
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        request = SimpleNamespace(
            method="POST", path_params={"phone_number_id": PNID},
            headers={"X-Hub-Signature-256": digest}, query_params={},
        )
        assert verify(request, raw, creds=_creds()) is False

    def test_uppercase_hex_signature_accepted(self):
        raw = b"{}"
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        request = SimpleNamespace(
            method="POST", path_params={"phone_number_id": PNID},
            headers={"X-Hub-Signature-256": "sha256=" + digest.upper()},
            query_params={},
        )
        assert verify(request, raw, creds=_creds()) is True

    def test_rejects_when_app_secret_missing(self):
        raw = b"{}"
        assert verify(_post_request(raw), raw, creds=_creds(app_secret="")) is False

    def test_rejects_oversize_body_before_crypto(self):
        raw = b"x" * (3 * 1024 * 1024 + 1)
        assert verify(_post_request(raw), raw, creds=_creds()) is False


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


class TestParse:
    def test_text_message(self):
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg is not None
        assert msg.text == "Hi!"
        assert msg.identifier == WA_ID
        assert msg.platform_user_id == WA_ID
        assert msg.user_name == "Jessica Laverdetman"
        assert msg.is_dm is True
        assert msg.visibility == "dm"
        assert msg.thread_key is None
        assert msg.is_bot is False

    def test_ts_is_the_wamid_not_the_timestamp(self):
        # The WhatsApp timestamp is second-resolution and would collide across
        # senders in the shared dedup cache; the wamid is globally unique.
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg.ts.startswith("wamid.")

    def test_source_carries_tenant_and_wamid(self):
        # ack_received receives only (msg, creds, config) — no routing, no
        # identifier — so everything it needs must ride on msg.source.
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg.source["phone_number_id"] == PNID
        assert msg.source["wamid"] == msg.ts

    def test_tenant_mismatch_is_dropped(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_wrong_object_dropped(self):
        body = _text_message()
        body["object"] = "page"
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_non_messages_field_dropped(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["field"] = "message_template_status_update"
        assert parse(body, creds=_creds(), identifier=PNID) is None

    @pytest.mark.parametrize(
        "msg_type", ["reaction", "system", "unsupported", "order", "location", "contacts"],
    )
    def test_non_message_types_return_none(self, msg_type):
        # Hermes maps these to TEXT with body="", so a thumbs-up starts a real
        # agent turn with an empty prompt.
        body = _text_message(type=msg_type)
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("text")
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_statuses_only_payload_returns_none(self):
        body = _text_message()
        value = body["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [{
            "id": "wamid.OUT1", "status": "failed", "recipient_id": WA_ID,
            "errors": [{"code": 131047, "title": "Re-engagement message"}],
        }]
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_statuses_logging_failure_does_not_raise(self, monkeypatch):
        # A parse exception becomes a 400 and a Meta retry loop.
        import surogates.channels.platforms.whatsapp as wa

        def _boom(*args, **kwargs):
            raise RuntimeError("log sink down")

        monkeypatch.setattr(wa.logger, "warning", _boom)
        body = _text_message()
        value = body["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [{"id": "w", "status": "failed"}]
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_image_message_produces_file_ref(self):
        body = _text_message(
            type="image",
            image={"id": "media_image_abc", "mime_type": "image/jpeg",
                   "caption": "look at this"},
        )
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("text")
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg is not None
        assert msg.text == "look at this"
        assert msg.kind == "image"
        assert len(msg.files) == 1
        assert msg.files[0].file_id == "media_image_abc"
        assert msg.files[0].mime_type == "image/jpeg"

    def test_document_uses_filename(self):
        body = _text_message(
            type="document",
            document={"id": "media_doc_abc", "mime_type": "text/plain",
                      "filename": "notes.txt"},
        )
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("text")
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg.files[0].filename == "notes.txt"

    def test_missing_sender_is_refused(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("from")
        assert parse(body, creds=_creds(), identifier=PNID) is None

    def test_unicode_body_preserved(self):
        msg = parse(
            _text_message(text={"body": "héllo 👋 مرحبا"}),
            creds=_creds(), identifier=PNID,
        )
        assert msg.text == "héllo 👋 مرحبا"

    def test_multi_message_batch_returns_first(self):
        # The framework processes ONE InboundMessage per webhook; when Meta
        # coalesces several user messages into one notification, v1 delivers
        # the first and WARN-logs the drop (recorded in spec §5.3/§10).
        body = _text_message()
        value = body["entry"][0]["changes"][0]["value"]
        value["messages"].append({
            "from": WA_ID, "id": "wamid.SECOND", "timestamp": "1758254145",
            "text": {"body": "second"}, "type": "text",
        })
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg.text == "Hi!"


# ---------------------------------------------------------------------------
# Platform object + descriptor
# ---------------------------------------------------------------------------


class TestWhatsAppPlatform:
    def test_kind_and_topology(self):
        p = WhatsAppPlatform()
        assert p.kind == "whatsapp"
        assert p.topology == "webhook"

    def test_declares_get_handshake(self):
        assert WhatsAppPlatform().handshake_get is True

    def test_route_path_template_when_no_identifier(self):
        assert WhatsAppPlatform().route_path() == "/whatsapp/{phone_number_id}"

    def test_route_path_concrete_with_identifier(self):
        assert WhatsAppPlatform().route_path(PNID) == f"/whatsapp/{PNID}"

    def test_no_supports_edit(self):
        # WhatsApp cannot edit a sent message.
        assert getattr(WhatsAppPlatform(), "supports_edit", False) is False

    def test_descriptor_vault_refs(self):
        refs = WhatsAppPlatform().descriptor.vault_refs(PNID)
        assert refs == {
            "access_token": "access_token",
            "app_secret": "app_secret",
            "verify_token": "verify_token",
        }

    def test_descriptor_registration_is_manual(self):
        # Meta has no setWebhook equivalent for the callback URL.
        assert WhatsAppPlatform().descriptor.webhook_registration == "manual"
        assert WhatsAppPlatform().descriptor.register_webhook is None

    def test_descriptor_config_keys_match_provisioner(self):
        # These names are the contract with the ops provisioner's config blob.
        assert set(WhatsAppPlatform().descriptor.config_keys) == {
            "require_mention", "allow_bots", "identity_policy",
            "waba_id", "api_version",
        }


class TestWhatsAppRegistration:
    def test_registered_in_registry(self):
        from surogates.channels.registry import registry
        assert registry.get("whatsapp") is not None
