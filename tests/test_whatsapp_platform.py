"""Tests for the WhatsApp Business Cloud API channel platform.

Written BEFORE the implementation module exists (TDD).  Mirrors
tests/test_telegram_platform.py in structure.
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
from types import SimpleNamespace

import httpx
import pytest
import respx

from surogates.channels.channel_media import OutboundFile
from surogates.channels.platforms.whatsapp import (
    WhatsAppPlatform,
    identifier_of,
    parse,
    verify,
)
from surogates.channels.platforms.whatsapp_api import DEFAULT_API_VERSION, graph_url
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
        "phone_number_id": PNID,
        # Empty exercises the DEFAULT_API_VERSION fallback; tests that pin a
        # version pass _creds(api_version="v25.0").
        "api_version": "",
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
            "phone_number_id": "phone_number_id",
            "api_version": "api_version",
        }

    def test_descriptor_registration_is_manual(self):
        # Meta has no setWebhook equivalent for the callback URL.
        assert WhatsAppPlatform().descriptor.webhook_registration == "manual"
        assert WhatsAppPlatform().descriptor.register_webhook is None

    def test_descriptor_config_keys_match_provisioner(self):
        # These names are the contract with the ops provisioner's config blob.
        assert set(WhatsAppPlatform().descriptor.config_keys) == {
            "identity_policy", "waba_id", "api_version",
        }

    def test_no_unreachable_gating_keys(self):
        # require_mention and allow_bots can never fire here: parse always
        # sets is_dm=True (so the mention gate short-circuits) and
        # is_bot=False (so the bot gate never runs).  Declaring them would
        # surface switches in Studio that do nothing.
        keys = set(WhatsAppPlatform().descriptor.config_keys)
        assert "require_mention" not in keys
        assert "allow_bots" not in keys


class TestWhatsAppRegistration:
    def test_registered_in_registry(self):
        from surogates.channels.registry import registry
        assert registry.get("whatsapp") is not None


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

MESSAGES_URL = graph_url(PNID, "messages")


def _item(content: str, **payload_extra):
    """An outbox row double: only .destination and .payload are read."""
    payload = {"content": content}
    payload.update(payload_extra)
    return SimpleNamespace(
        destination={
            "wa_id": WA_ID,
            "phone_number_id": PNID,
            "channel_identifier": PNID,
        },
        payload=payload,
    )


class TestWhatsAppSend:
    @pytest.mark.asyncio
    async def test_sends_text_and_returns_wamid(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.OUT1"}]},
                )
            )
            result = await p.send(_item("hello"), creds=_creds())
        assert result.success is True
        assert result.message_id == "wamid.OUT1"

    @pytest.mark.asyncio
    async def test_payload_shape(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            await p.send(_item("hi"), creds=_creds())
        body = _json.loads(route.calls[0].request.content)
        assert body["messaging_product"] == "whatsapp"
        assert body["recipient_type"] == "individual"
        assert body["to"] == WA_ID
        assert body["type"] == "text"
        assert body["text"]["body"] == "hi"

    @pytest.mark.asyncio
    async def test_markdown_is_transcoded(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            await p.send(_item("**bold**"), creds=_creds())
        body = _json.loads(route.calls[0].request.content)
        assert body["text"]["body"] == "*bold*"

    @pytest.mark.asyncio
    async def test_long_text_is_split(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            result = await p.send(_item("a " * 4000), creds=_creds())
            assert len(router.calls) >= 2
        assert result.success is True

    @pytest.mark.asyncio
    async def test_empty_content_sends_nothing_and_succeeds(self):
        # success=True/message_id=None is the correct terminal state:
        # _deliver_item has two branches and never reads SendResult.retryable,
        # so success=False would requeue an unsendable item for 30 minutes.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=False) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={})
            )
            result = await p.send(_item("   "), creds=_creds())
        assert result.success is True
        assert result.message_id is None
        assert len(route.calls) == 0

    @pytest.mark.asyncio
    async def test_failure_returns_formatted_error(self):
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    400, json={"error": {"message": "Re-engagement message",
                                         "code": 131047}},
                )
            )
            result = await p.send(_item("hi"), creds=_creds())
        assert result.success is False
        assert result.error == (
            "graph error 131047 (HTTP 400): Re-engagement message"
        )

    @pytest.mark.asyncio
    async def test_partial_send_reports_delivered_prefix(self):
        # A mid-sequence failure must report success with the last delivered
        # id, so a retry does not duplicate already-delivered chunks.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(
                side_effect=[
                    httpx.Response(200, json={"messages": [{"id": "wamid.C1"}]}),
                    httpx.Response(500, json={"error": {"message": "boom"}}),
                ]
            )
            result = await p.send(_item("a " * 4000), creds=_creds())
        assert result.success is True
        assert result.message_id == "wamid.C1"

    @pytest.mark.asyncio
    async def test_uses_api_version_from_creds(self):
        # api_version rides in creds (stored by the provisioner alongside
        # phone_number_id): the outbound path receives only (item, creds),
        # and session config never carries routing config.
        p = WhatsAppPlatform()
        url = graph_url(PNID, "messages", api_version="v25.0")
        with respx.mock(assert_all_called=True) as router:
            router.post(url).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.X"}]},
                )
            )
            result = await p.send(_item("hi"), creds=_creds(api_version="v25.0"))
        assert result.success is True


# ---------------------------------------------------------------------------
# ask_user_question — text-mode prompt
# ---------------------------------------------------------------------------


def _questions(raw: list[dict]) -> list[dict]:
    """Normalise questions the way ``ask_user_question`` does.

    Building the fixture through the real validator is deliberate: hand-written
    payloads let the platform read the wrong keys while the test still passes.
    ``INBOX_INPUT_REQUIRED`` carries exactly this shape.
    """
    from surogates.tools.builtin.ask_user_question import _validate_questions

    return _validate_questions(raw)


class TestWhatsAppInputPrompt:
    def test_fixture_uses_the_canonical_schema(self):
        # Guards the bug this class exists to prevent: the keys are
        # ``prompt``/``choices``, never ``question``/``options``.
        [q] = _questions([
            {"prompt": "Which environment?",
             "choices": [{"label": "staging"}, {"label": "production"}]},
        ])
        assert q["prompt"] == "Which environment?"
        assert [c["label"] for c in q["choices"]] == ["staging", "production"]

    @pytest.mark.asyncio
    async def test_renders_the_question_and_its_choices(self):
        p = WhatsAppPlatform()
        item = _item(
            "",
            input_prompt=True,
            tool_call_id="tc1",
            context="Need a decision.",
            questions=_questions([{
                "prompt": "Which environment?",
                "choices": [{"label": "staging"}, {"label": "production"}],
            }]),
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.Q1"}]},
                )
            )
            result = await p.send(item, creds=_creds())
        sent = _json.loads(route.calls[0].request.content)["text"]["body"]
        assert "Which environment?" in sent
        assert "Need a decision." in sent
        assert "staging" in sent
        assert "production" in sent
        assert result.success is True

    @pytest.mark.asyncio
    async def test_choices_are_answerable_by_label(self):
        # resolve_text_answer matches labels, so the rendered choice text must
        # be the label verbatim — numbering it would invite an unmappable "1".
        from surogates.channels.platforms.telegram_interactive import (
            resolve_text_answer,
        )

        p = WhatsAppPlatform()
        questions = _questions([{
            "prompt": "Which environment?",
            "choices": [{"label": "staging"}, {"label": "production"}],
        }])
        item = _item("", input_prompt=True, tool_call_id="tc1", context="",
                     questions=questions)
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.Q1"}]},
                )
            )
            await p.send(item, creds=_creds())
        sent = _json.loads(route.calls[0].request.content)["text"]["body"]

        # Every label the user can read back is one the resolver accepts.
        for label in ("staging", "production"):
            assert label in sent
            [answer] = resolve_text_answer(questions, label)
            assert answer.get("is_other") is not True

    @pytest.mark.asyncio
    async def test_prompt_without_choices_still_sends_the_question(self):
        p = WhatsAppPlatform()
        item = _item(
            "", input_prompt=True, tool_call_id="tc2", context="",
            questions=_questions([{"prompt": "What is the deploy tag?"}]),
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.Q2"}]},
                )
            )
            await p.send(item, creds=_creds())
        sent = _json.loads(route.calls[0].request.content)["text"]["body"]
        assert "What is the deploy tag?" in sent


# ---------------------------------------------------------------------------
# ack_received — read receipt + typing in one call
# ---------------------------------------------------------------------------


class TestAckReceived:
    @pytest.mark.asyncio
    async def test_marks_read_and_sets_typing(self):
        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"success": True})
            )
            await p.ack_received(msg, creds=_creds(), config={})
        body = _json.loads(route.calls[0].request.content)
        assert body == {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": msg.source["wamid"],
            "typing_indicator": {"type": "text"},
        }

    @pytest.mark.asyncio
    async def test_no_call_without_wamid(self):
        p = WhatsAppPlatform()
        msg = SimpleNamespace(identifier=WA_ID, source={"phone_number_id": PNID})
        with respx.mock(assert_all_called=False) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={})
            )
            await p.ack_received(msg, creds=_creds(), config={})
        assert len(route.calls) == 0

    @pytest.mark.asyncio
    async def test_never_raises_on_transport_error(self):
        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        with respx.mock(assert_all_called=True) as router:
            router.post(MESSAGES_URL).mock(side_effect=httpx.ConnectError("down"))
            await p.ack_received(msg, creds=_creds(), config={})


# ---------------------------------------------------------------------------
# download_file
# ---------------------------------------------------------------------------


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_two_hop_fetch(self):
        from surogates.channels.platforms.whatsapp_api import GRAPH_API_BASE

        p = WhatsAppPlatform()
        meta_url = f"{GRAPH_API_BASE}/{DEFAULT_API_VERSION}/media_abc"
        blob_url = "https://lookaside.fbsbx.com/whatsapp/m/xyz"
        with respx.mock(assert_all_called=True) as router:
            router.get(meta_url).mock(
                return_value=httpx.Response(
                    200, json={"url": blob_url, "mime_type": "image/jpeg",
                               "file_size": 5},
                )
            )
            router.get(blob_url).mock(
                return_value=httpx.Response(200, content=b"BYTES")
            )
            data = await p.download_file(
                creds=_creds(), url="media_abc", max_bytes=1024,
            )
        assert data == b"BYTES"

    @pytest.mark.asyncio
    async def test_returns_none_without_token(self):
        p = WhatsAppPlatform()
        assert await p.download_file(
            creds={"access_token": ""}, url="media_abc", max_bytes=1024,
        ) is None


# ---------------------------------------------------------------------------
# send_private / post_input_nudge
# ---------------------------------------------------------------------------


class TestSendPrivateAndNudge:
    @pytest.mark.asyncio
    async def test_send_private_delivers_and_returns_true(self):
        # Every WhatsApp conversation is already a DM, so this is a plain send.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.P1"}]},
                )
            )
            ok = await p.send_private(
                _creds(), sender_id=WA_ID, chat_id=WA_ID, is_dm=True,
                text="Your link code is ABCD-1234",
            )
        assert ok is True
        body = _json.loads(route.calls[0].request.content)
        assert "ABCD-1234" in body["text"]["body"]

    @pytest.mark.asyncio
    async def test_send_private_false_without_phone_number_id(self):
        p = WhatsAppPlatform()
        ok = await p.send_private(
            {"access_token": ACCESS_TOKEN}, sender_id=WA_ID, chat_id=WA_ID,
            is_dm=True, text="hi",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_post_input_nudge_sends_text_and_returns_id(self):
        # Delivers the /stop ack and the allowance-block notice with its buy
        # link; runner.py getattr-guards it, so omitting it fails silently.
        p = WhatsAppPlatform()
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.N1"}]},
                )
            )
            result = await p.post_input_nudge(
                creds=_creds(), channel=WA_ID, thread_ts=None,
                text="⏹ Stopping the current run…",
            )
        assert result == "wamid.N1"
        body = _json.loads(route.calls[0].request.content)
        assert body["to"] == WA_ID
        assert "Stopping" in body["text"]["body"]


# ---------------------------------------------------------------------------
# send_files — two-step Graph upload
# ---------------------------------------------------------------------------

MEDIA_URL = graph_url(PNID, "media")


class TestSendFiles:
    @pytest.mark.asyncio
    async def test_uploads_then_sends_and_returns_media_ids(self):
        p = WhatsAppPlatform()
        files = [OutboundFile(filename="chart.png", mime_type="image/png", data=b"PNG")]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "media_up1"})
            )
            send = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.M1"}]},
                )
            )
            uploaded = await p.send_files(_item(""), creds=_creds(), files=files)
        assert uploaded == ["media_up1"]
        body = _json.loads(send.calls[0].request.content)
        assert body["type"] == "image"
        assert body["image"]["id"] == "media_up1"

    @pytest.mark.asyncio
    async def test_document_carries_filename(self):
        p = WhatsAppPlatform()
        files = [OutboundFile(filename="notes.pdf", mime_type="application/pdf",
                              data=b"PDF")]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "media_doc"})
            )
            send = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.D1"}]},
                )
            )
            await p.send_files(_item(""), creds=_creds(), files=files)
        body = _json.loads(send.calls[0].request.content)
        assert body["type"] == "document"
        assert body["document"]["filename"] == "notes.pdf"

    @pytest.mark.asyncio
    async def test_oversize_file_is_skipped_not_raised(self):
        p = WhatsAppPlatform()
        files = [
            OutboundFile(filename="big.png", mime_type="image/png",
                         data=b"x" * (6 * 1024 * 1024)),
            OutboundFile(filename="ok.png", mime_type="image/png", data=b"PNG"),
        ]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "media_ok"})
            )
            router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.OK"}]},
                )
            )
            uploaded = await p.send_files(_item(""), creds=_creds(), files=files)
        assert uploaded == ["media_ok"]

    @pytest.mark.asyncio
    async def test_no_caption_so_surrounding_text_is_not_repeated(self):
        # _deliver_item posts payload["content"] as its own message before
        # calling send_files, so captioning the attachment with it would show
        # the same sentence twice.
        p = WhatsAppPlatform()
        item = _item("Here is the chart.")
        files = [OutboundFile(filename="a.png", mime_type="image/png", data=b"P")]
        with respx.mock(assert_all_called=True) as router:
            router.post(MEDIA_URL).mock(
                return_value=httpx.Response(200, json={"id": "m1"})
            )
            send = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.C"}]},
                )
            )
            await p.send_files(item, creds=_creds(), files=files)
        body = _json.loads(send.calls[0].request.content)
        assert "caption" not in body["image"]

    @pytest.mark.asyncio
    async def test_returns_empty_without_token(self):
        p = WhatsAppPlatform()
        files = [OutboundFile(filename="a.png", mime_type="image/png", data=b"P")]
        assert await p.send_files(
            _item(""), creds={"access_token": ""}, files=files,
        ) == []


# ---------------------------------------------------------------------------
# Pending-input tuple + outbox destination
# ---------------------------------------------------------------------------


class TestWhatsAppPipelineWiring:
    def test_pending_input_tuple_includes_whatsapp(self):
        # The non-Slack fallthrough in inbound.py is the plain-text answer
        # path (resolve_text_answer); joining the tuple opts in for free.
        import inspect

        import surogates.channels.inbound as inbound

        source = inspect.getsource(inbound.ChannelInboundPipeline.handle)
        assert "whatsapp" in source, (
            "whatsapp missing from the pending-input platform tuple: a typed "
            "answer would be treated as a new message and never resolve"
        )

    def test_thread_dest_fields_has_whatsapp(self):
        from surogates.session.store import _THREAD_DEST_FIELDS

        assert "whatsapp" in _THREAD_DEST_FIELDS


# ---------------------------------------------------------------------------
# wa_id normalisation
# ---------------------------------------------------------------------------


class TestWaIdNormalisation:
    def test_leading_plus_is_stripped(self):
        # identity.py's shadow-user email only strips "@", so a "+" would land
        # inside the email local part.
        body = _text_message(**{"from": f"+{WA_ID}"})
        msg = parse(body, creds=_creds(), identifier=PNID)
        assert msg.platform_user_id == WA_ID
        assert msg.identifier == WA_ID

    def test_bare_digits_unchanged(self):
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        assert msg.platform_user_id == WA_ID

    def test_absent_type_is_not_treated_as_text(self):
        body = _text_message()
        body["entry"][0]["changes"][0]["value"]["messages"][0].pop("type")
        assert parse(body, creds=_creds(), identifier=PNID) is None
