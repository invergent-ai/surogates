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
    async def test_short_choices_go_out_as_reply_buttons(self):
        p = WhatsAppPlatform()
        item = _item(
            "",
            input_prompt=True,
            tool_call_id="tc1",
            questions=_questions([{
                "prompt": "Cum vă simțiți azi?",
                "choices": [{"label": "Bine"}, {"label": "Obosit"}, {"label": "Rău"}],
            }]),
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(
                    200, json={"messages": [{"id": "wamid.Q1"}]},
                )
            )
            result = await p.send(item, creds=_creds())
        sent = _json.loads(route.calls[0].request.content)
        assert sent["type"] == "interactive"
        assert sent["interactive"] == {
            "type": "button",
            "body": {"text": "Cum vă simțiți azi?"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "choice:0", "title": "Bine"}},
                {"type": "reply", "reply": {"id": "choice:1", "title": "Obosit"}},
                {"type": "reply", "reply": {"id": "choice:2", "title": "Rău"}},
            ]},
        }
        assert result.success is True and result.message_id == "wamid.Q1"

    @pytest.mark.asyncio
    async def test_four_choices_go_out_as_a_list(self):
        p = WhatsAppPlatform()
        item = _item(
            "",
            input_prompt=True,
            tool_call_id="tc1",
            questions=_questions([{
                "prompt": "Ce simptom aveți?",
                "button_label": "Alegeți",
                "choices": [
                    {"label": "Durere în piept", "description": "La efort sau în repaus"},
                    {"label": "Dispnee"},
                    {"label": "Palpitații"},
                    {"label": "Niciunul"},
                ],
            }]),
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(MESSAGES_URL).mock(
                return_value=httpx.Response(200, json={"messages": [{"id": "wamid.L"}]}),
            )
            await p.send(item, creds=_creds())
        interactive = _json.loads(route.calls[0].request.content)["interactive"]
        assert interactive["type"] == "list"
        assert interactive["action"]["button"] == "Alegeți"
        [section] = interactive["action"]["sections"]
        assert section["rows"][0] == {
            "id": "choice:0", "title": "Durere în piept",
            "description": "La efort sau în repaus",
        }
        assert [r["title"] for r in section["rows"]] == [
            "Durere în piept", "Dispnee", "Palpitații", "Niciunul",
        ]

    def test_list_button_defaults_to_a_glyph_not_english(self):
        from surogates.channels.platforms.whatsapp import _interactive_prompt

        interactive = _interactive_prompt({"questions": [{
            "prompt": "?",
            "choices": [{"label": str(n)} for n in range(4)],
        }]})
        assert interactive["action"]["button"] == "☰"

    @pytest.mark.parametrize("payload", [
        # several questions
        {"questions": [
            {"prompt": "a", "choices": [{"label": "x"}]},
            {"prompt": "b", "choices": [{"label": "y"}]},
        ]},
        # open-ended
        {"questions": [{"prompt": "Când a avut loc procedura?"}]},
        # a label too long for a list row
        {"questions": [{"prompt": "a", "choices": [{"label": "x" * 25}]}]},
        # a description too long for a list row
        {"questions": [{"prompt": "a", "choices": [{"label": "x", "description": "d" * 73}]}]},
        # a body over the interactive cap
        {"questions": [{"prompt": "p" * 1025, "choices": [{"label": "x"}]}]},
    ])
    def test_prompts_beyond_meta_limits_stay_text(self, payload):
        from surogates.channels.platforms.whatsapp import _interactive_prompt

        assert _interactive_prompt(payload) is None

    @pytest.mark.asyncio
    async def test_choices_are_answerable_by_label(self):
        # resolve_text_answer matches labels, so the rendered choice text must
        # be the label verbatim — numbering it would invite an unmappable "1".
        from surogates.channels.platforms.telegram_interactive import (
            resolve_text_answer,
        )

        p = WhatsAppPlatform()
        questions = _questions([
            {"prompt": "Which environment?",
             "choices": [{"label": "staging"}, {"label": "production"}]},
            {"prompt": "Which region?"},
        ])
        item = _item("", input_prompt=True, tool_call_id="tc1", questions=questions)
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
            [answer] = resolve_text_answer(questions[:1], label)
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


class TestKeepTyping:
    """Meta drops the pip after 25 s or on any outbound message; the turn
    can run for minutes."""

    @pytest.fixture
    def calls(self, monkeypatch):
        import surogates.channels.platforms.whatsapp as wa

        sent: list[dict] = []

        async def fake_send(_http, *, payload, **_kw):
            sent.append(payload)
            if payload.get("status") == "read":
                return None, None
            return "wamid.OUT", None

        monkeypatch.setattr(wa, "send_message", fake_send)
        monkeypatch.setattr(wa, "_TYPING_EVERY_S", 0.01)
        return sent

    @staticmethod
    def _typing(calls):
        return [c for c in calls if c.get("status") == "read"]

    @staticmethod
    def _active(flag):
        async def turn_active():
            return flag["on"]
        return turn_active

    @pytest.mark.asyncio
    async def test_resends_until_the_turn_ends(self, calls):
        import asyncio

        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        flag = {"on": True}
        p.keep_typing(msg, creds=_creds(), turn_active=self._active(flag))
        await asyncio.sleep(0.05)
        assert self._typing(calls)[0] == {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": msg.source["wamid"],
            "typing_indicator": {"type": "text"},
        }
        assert len(self._typing(calls)) >= 3
        flag["on"] = False
        await asyncio.sleep(0.03)
        assert p._typing == {}
        n = len(self._typing(calls))
        await asyncio.sleep(0.03)
        assert len(self._typing(calls)) == n

    @pytest.mark.asyncio
    async def test_reply_or_question_stops_it_mid_turn_text_rearms_it(self, calls):
        import asyncio

        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        p.keep_typing(msg, creds=_creds(), turn_active=self._active({"on": True}))
        await asyncio.sleep(0)
        for mid_turn in (_item("still running", code_run="r1"),
                         _item("Sunați la 112.", intermediate=True)):
            await p.send(mid_turn, creds=_creds())
            await asyncio.sleep(0)
            assert calls[-1].get("status") == "read"
        await p.send(_item("Gata."), creds=_creds())
        assert p._typing == {}
        n = len(calls)
        await asyncio.sleep(0.03)
        assert len(calls) == n

        p.keep_typing(msg, creds=_creds(), turn_active=self._active({"on": True}))
        await p.send(
            _item("", input_prompt=True, questions=[{"prompt": "Da?"}]), creds=_creds(),
        )
        assert p._typing == {}

    @pytest.mark.asyncio
    async def test_stops_at_the_cap(self, calls, monkeypatch):
        import asyncio

        import surogates.channels.platforms.whatsapp as wa

        monkeypatch.setattr(wa, "_TYPING_CAP_S", 0.02)
        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        p.keep_typing(msg, creds=_creds(), turn_active=self._active({"on": True}))
        await asyncio.sleep(0.08)
        assert p._typing == {}

    @pytest.mark.asyncio
    async def test_no_call_without_wamid(self, calls):
        import asyncio

        p = WhatsAppPlatform()
        msg = SimpleNamespace(identifier=WA_ID, source={"phone_number_id": PNID})
        p.keep_typing(msg, creds=_creds(), turn_active=self._active({"on": True}))
        await asyncio.sleep(0.02)
        assert calls == []

    @pytest.mark.asyncio
    async def test_never_raises_when_the_turn_check_fails(self, calls):
        import asyncio

        async def broken():
            raise RuntimeError("db down")

        p = WhatsAppPlatform()
        msg = parse(_text_message(), creds=_creds(), identifier=PNID)
        p.keep_typing(msg, creds=_creds(), turn_active=broken)
        await asyncio.sleep(0.05)
        assert p._typing == {}


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


# ---------------------------------------------------------------------------
# App-level webhook
# ---------------------------------------------------------------------------

# surogate-ops pins the same key → token pair (whatsapp_app_verify_token in
# channel_provisioning). If this vector changes, change it there too, or Meta
# rejects the token the connect page shows.
APP_KEY = "ZmFrZS1rZXktZm9yLXRlc3RzLW9ubHktMzItYnl0ZXM="
APP_TOKEN = "9f23d0744f7fd2d5db656e2df1baac5a"


def test_app_verify_token_matches_the_vector_ops_pins():
    from surogates.channels.platforms.whatsapp import app_verify_token

    assert app_verify_token(APP_KEY) == APP_TOKEN
    # A secret mounted with a trailing newline must not change the token.
    assert app_verify_token(APP_KEY + "\n") == APP_TOKEN


def _app_level_client(encryption_key: str = APP_KEY) -> httpx.AsyncClient:
    from surogates.channels.dispatcher import ChannelWebhookDispatcher
    from surogates.channels.registry import ChannelRegistry

    reg = ChannelRegistry()
    reg.register(WhatsAppPlatform())
    app = ChannelWebhookDispatcher(
        cache=None,
        vault=None,
        pipeline=None,
        deps_factory=lambda *a: None,
        settings=SimpleNamespace(
            channels={"whatsapp": SimpleNamespace(enabled=True)},
            encryption_key=encryption_key,
        ),
        registry=reg,
    ).build_app()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test",
    )


def _handshake(token: str) -> dict:
    return {
        "hub.mode": "subscribe",
        "hub.verify_token": token,
        "hub.challenge": "1158201444",
    }


async def test_app_level_handshake_echoes_the_challenge():
    async with _app_level_client() as c:
        r = await c.get("/whatsapp", params=_handshake(APP_TOKEN))
    assert r.status_code == 200
    assert r.text == "1158201444"


async def test_app_level_handshake_rejects_a_wrong_token():
    async with _app_level_client() as c:
        r = await c.get("/whatsapp", params=_handshake("wrong"))
    assert r.status_code == 401


async def test_app_level_handshake_refuses_without_an_encryption_key():
    # No key means no token to compare against; an empty token must not pass.
    async with _app_level_client(encryption_key="") as c:
        r = await c.get("/whatsapp", params=_handshake(""))
    assert r.status_code == 401


async def test_app_level_event_is_acknowledged_and_dropped():
    # A non-200 makes Meta retry and eventually disable the app's webhook.
    async with _app_level_client() as c:
        r = await c.post("/whatsapp", json=_text_message())
    assert r.status_code == 200


def test_input_prompt_carries_no_english_chrome() -> None:
    """End users read this in their own language: only the agent's words
    and the choices go out."""
    from surogates.channels.platforms.whatsapp import _render_input_prompt

    text = _render_input_prompt({
        "context": "Am notat data procedurii.",
        "questions": [{"prompt": "Sunteți de acord?", "choices": [{"label": "Da"}]}],
    })
    assert text == "Am notat data procedurii.\nSunteți de acord?\n  • Da"


def test_channel_prompt_drops_the_models_own_context() -> None:
    """The model's ``context`` is its reasoning for the operator inbox: it
    reached patients as "Pasul 4 din skill-ul de înrolare: ..."."""
    from surogates.session.events import EventType
    from surogates.session.store import _build_channel_payload

    payload = _build_channel_payload(
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": "call_abc",
            "questions": [{"prompt": "Când a avut loc procedura?"}],
            "context": "Pasul 4 din skill-ul de înrolare: aflu data.",
        },
        "whatsapp",
    )
    assert payload["input_prompt"] is True
    assert "context" not in payload

    from surogates.channels.platforms.whatsapp import _render_input_prompt
    assert "Pasul 4" not in _render_input_prompt(payload)


def test_channel_prompt_keeps_the_rescued_reply() -> None:
    """A rescued final reply is the agent talking to the person; dropping it
    would leave only the bare question."""
    from surogates.session.events import EventType
    from surogates.session.inbox_payload import ASK_USER_QUESTION_RESCUE_PREFIX
    from surogates.session.store import _build_channel_payload

    payload = _build_channel_payload(
        EventType.INBOX_INPUT_REQUIRED,
        {
            "tool_call_id": f"{ASK_USER_QUESTION_RESCUE_PREFIX}x",
            "questions": [{"prompt": "Sunteți de acord?"}],
            "context": "Am notat data procedurii.",
        },
        "telegram",
    )
    assert payload["context"] == "Am notat data procedurii."


class TestInboundTaps:
    """Taps reach the agent as the text of what was tapped.  Shapes are
    Meta's webhook samples for each message type."""

    def _parse(self, **message):
        return parse(_text_message(**message), identifier=PNID)

    def test_reply_button_tap_is_its_title(self):
        msg = self._parse(
            type="interactive",
            context={"from": "15551797781", "id": "wamid.prompt"},
            interactive={
                "type": "button_reply",
                "button_reply": {"id": "choice:1", "title": "Obosit"},
            },
        )
        assert msg is not None
        assert msg.text == "Obosit"
        assert msg.kind == "text"

    def test_list_row_tap_is_its_title_without_the_description(self):
        from surogates.channels.platforms.telegram_interactive import resolve_text_answer

        msg = self._parse(
            type="interactive",
            interactive={
                "type": "list_reply",
                "list_reply": {
                    "id": "choice:0",
                    "title": "Durere în piept",
                    "description": "La efort sau în repaus",
                },
            },
        )
        assert msg.text == "Durere în piept"
        questions = _questions([{
            "prompt": "Ce simptom aveți?",
            "choices": [{"label": "Durere în piept"}, {"label": "Niciunul"}],
        }])
        [answer] = resolve_text_answer(questions, msg.text)
        assert answer.get("is_other") is not True

    def test_template_quick_reply_is_its_text(self):
        # The check-in opener's "Încep acum": dropping it let the check-in
        # expire as missed.
        msg = self._parse(
            type="button",
            context={"from": "15551797781", "id": "wamid.opener"},
            button={"payload": "Încep acum", "text": "Încep acum"},
        )
        assert msg is not None
        assert msg.text == "Încep acum"

    def test_flow_completion_is_rendered_as_key_value_lines(self):
        msg = self._parse(
            type="interactive",
            interactive={
                "type": "nfm_reply",
                "nfm_reply": {
                    "name": "flow",
                    "body": "Sent",
                    "response_json": _json.dumps({
                        "flow_token": "tok-123",
                        "tensiune": "130/85",
                        "simptome": ["oboseală", "amețeli"],
                    }),
                },
            },
        )
        assert msg.text == "tensiune: 130/85\nsimptome: oboseală, amețeli"

    def test_tap_without_text_is_ignored(self):
        assert self._parse(
            type="interactive", interactive={"type": "button_reply", "button_reply": {}},
        ) is None


class TestQuestionNotRepeated:
    """The reply ended in "Cum vă numiți? (numele și prenumele)" and the
    ask_user_question beside it asked exactly that: the patient saw it twice."""

    @staticmethod
    def _payload(content, *questions, channel="whatsapp"):
        from surogates.session.events import EventType
        from surogates.session.store import _build_channel_payload

        return _build_channel_payload(
            EventType.LLM_RESPONSE,
            {"message": {
                "role": "assistant",
                "content": content,
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "ask_user_question",
                    "arguments": _json.dumps({"questions": list(questions)}),
                }}],
            }},
            channel,
        )

    def test_the_closing_repeat_is_cut(self):
        payload = self._payload(
            "Mulțumesc.\n\nCum  vă numiți?\n(numele și prenumele)\n",
            {"prompt": "Cum vă numiți? (numele și prenumele)"},
        )
        assert payload["content"] == "Mulțumesc."

    def test_a_reply_that_is_only_the_question_sends_nothing(self):
        assert self._payload(
            "Sunteți de acord?",
            {"prompt": "Sunteți de acord?", "choices": [{"label": "Da"}, {"label": "Nu"}]},
        ) == {}

    @pytest.mark.parametrize("content, questions, channel", [
        # A paraphrase is not provably a repeat.
        ("Mulțumesc. Sunteți de acord?", [{"prompt": "Sunteți de acord să vă scriu?"}], "whatsapp"),
        # Only a whole closing sentence, never the tail of a word.
        ("Mulțumesc. NuSunteți de acord?", [{"prompt": "Sunteți de acord?"}], "whatsapp"),
        ("Mulțumesc. A? B?", [{"prompt": "A?"}, {"prompt": "B?"}], "whatsapp"),
        # The web chat shows no separate prompt text.
        ("Mulțumesc. Sunteți de acord?", [{"prompt": "Sunteți de acord?"}], "web"),
    ])
    def test_anything_else_is_kept(self, content, questions, channel):
        assert self._payload(content, *questions, channel=channel)["content"] == content
