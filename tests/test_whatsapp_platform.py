"""WhatsApp platform message, prompt and media delivery workflows."""

from __future__ import annotations

import json as _json
from types import SimpleNamespace

import httpx
import pytest
import respx

from surogates.channels.channel_media import OutboundFile
from surogates.channels.platforms.whatsapp import (
    WhatsAppPlatform,
    parse,
)
from surogates.channels.platforms.whatsapp_api import DEFAULT_API_VERSION, graph_url

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


def _questions(raw: list[dict]) -> list[dict]:
    """Normalise questions the way ``ask_user_question`` does.

    Building the fixture through the real validator is deliberate: hand-written
    payloads let the platform read the wrong keys while the test still passes.
    ``INBOX_INPUT_REQUIRED`` carries exactly this shape.
    """
    from surogates.tools.builtin.ask_user_question import _validate_questions

    return _validate_questions(raw)


class TestWhatsAppInputPrompt:

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
