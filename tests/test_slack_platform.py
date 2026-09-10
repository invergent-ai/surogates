"""Slack platform message delivery, enrichment, authentication and interactive requests."""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


SIGNING_SECRET = "test_signing_secret_ABC123"
APP_ID = "A0TESTAPPID"
BOT_USER_ID = "U0BOTUSER"
BOT_TOKEN = "xoxb-test-token"


def _slack_signature(signing_secret: str, timestamp: str, raw_body: bytes) -> str:
    """Compute the expected X-Slack-Signature value."""
    basestring = f"v0:{timestamp}:{raw_body.decode()}"
    mac = hmac.new(
        signing_secret.encode(), basestring.encode(), hashlib.sha256
    )
    return f"v0={mac.hexdigest()}"


def _make_request(
    *,
    app_id: str = APP_ID,
    timestamp: str | None = None,
    signature: str | None = None,
    raw_body: bytes = b"{}",
    signing_secret: str = SIGNING_SECRET,
) -> SimpleNamespace:
    """Build a fake Starlette-like request with path_params and headers."""
    ts = timestamp or str(int(time.time()))
    sig = signature or _slack_signature(signing_secret, ts, raw_body)
    return SimpleNamespace(
        path_params={"app_id": app_id},
        headers={
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
        },
    )


def _creds(
    signing_secret: str = SIGNING_SECRET,
    bot_token: str = BOT_TOKEN,
) -> dict:
    return {"signing_secret": signing_secret, "bot_token": bot_token}


from surogates.channels.platforms.slack import (  # noqa: E402
    SlackPlatform,
)


class TestSlackPlatformDelegation:

    def test_verify_delegates_to_module_function_and_accepts_valid_sig(self):
        import json
        p = SlackPlatform()
        body_dict = {"type": "event_callback", "api_app_id": APP_ID}
        raw_body = json.dumps(body_dict).encode()
        request = _make_request(raw_body=raw_body)
        result = p.verify(request, raw_body, creds=_creds())
        assert result is True

    def test_verify_rejects_bad_signature(self):
        import json
        p = SlackPlatform()
        body_dict = {"type": "event_callback", "api_app_id": APP_ID}
        raw_body = json.dumps(body_dict).encode()
        request = _make_request(raw_body=raw_body, signing_secret="wrong")
        result = p.verify(request, raw_body, creds=_creds())
        assert result is False


class TestSlackPlatformParse:
    """SlackPlatform.parse(body, creds=...) resolves bot_user_id via auth.test."""

    def _make_body(self, text: str = "hello bot", user: str = "U1") -> dict:
        return {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "message",
                "text": text,
                "user": user,
                "channel": "C123",
                "channel_type": "channel",
                "ts": "1700000100.000100",
            },
        }

    @pytest.mark.asyncio
    async def test_parse_calls_auth_test_for_bot_user_id(self):
        """parse calls auth.test to discover bot_user_id."""
        import asyncio
        p = SlackPlatform()

        mock_client = AsyncMock()
        mock_client.auth_test.return_value = {"user_id": BOT_USER_ID}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = p.parse(self._make_body(), creds=_creds())
            if asyncio.iscoroutine(result):
                result = await result

        assert result is not None
        mock_client.auth_test.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_caches_auth_test_across_calls(self):
        """auth.test is called once per bot token, not once per parse call."""
        import asyncio
        p = SlackPlatform()

        mock_client = AsyncMock()
        mock_client.auth_test.return_value = {"user_id": BOT_USER_ID}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            for _ in range(3):
                result = p.parse(self._make_body(), creds=_creds())
                if asyncio.iscoroutine(result):
                    result = await result

        # auth.test should only be called once regardless of how many parse calls
        assert mock_client.auth_test.call_count == 1

    @pytest.mark.asyncio
    async def test_parse_detects_mention(self):
        import asyncio
        p = SlackPlatform()

        mock_client = AsyncMock()
        mock_client.auth_test.return_value = {"user_id": BOT_USER_ID}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = p.parse(
                self._make_body(text=f"<@{BOT_USER_ID}> help me"),
                creds=_creds(),
            )
            if asyncio.iscoroutine(result):
                result = await result

        assert result is not None
        assert result.is_mention is True

    @pytest.mark.asyncio
    async def test_parse_returns_none_for_bot_message(self):
        import asyncio
        p = SlackPlatform()

        mock_client = AsyncMock()
        mock_client.auth_test.return_value = {"user_id": BOT_USER_ID}
        bot_body = {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "message",
                "bot_id": "B123",
                "text": "I am a bot",
                "channel": "C123",
                "ts": "1700000200.000001",
            },
        }

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = p.parse(bot_body, creds=_creds())
            if asyncio.iscoroutine(result):
                result = await result

        assert result is None


class TestSlackPlatformSend:
    """send posts to chat.postMessage and returns SendResult."""

    def _make_outbox_item(
        self,
        channel_id: str = "C123",
        text: str = "hello",
        thread_ts: str | None = None,
    ):
        dest = {"channel_id": channel_id}
        if thread_ts:
            dest["thread_ts"] = thread_ts
        return SimpleNamespace(
            destination=dest,
            payload={"content": text},
        )

    @pytest.mark.asyncio
    async def test_send_calls_chat_post_message(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ok": True, "ts": "1700000001.000001"}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.send(self._make_outbox_item(), creds=_creds())

        mock_client.chat_postMessage.assert_called_once()
        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["text"] == "hello"

    @pytest.mark.asyncio
    async def test_send_includes_thread_ts_when_present(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ok": True, "ts": "1700000001.000002"}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.send(
                self._make_outbox_item(thread_ts="1700000000.000001"),
                creds=_creds(),
            )

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert call_kwargs.get("thread_ts") == "1700000000.000001"

    @pytest.mark.asyncio
    async def test_send_omits_thread_ts_when_absent(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ok": True, "ts": "1700000001.000003"}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.send(self._make_outbox_item(), creds=_creds())

        call_kwargs = mock_client.chat_postMessage.call_args.kwargs
        assert "thread_ts" not in call_kwargs

    @pytest.mark.asyncio
    async def test_send_success_returns_send_result_with_ts(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ok": True, "ts": "1700000001.000010"}

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.send(self._make_outbox_item(), creds=_creds())

        assert result.success is True
        assert result.message_id == "1700000001.000010"

    @pytest.mark.asyncio
    async def test_send_exception_returns_send_result_failure(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.chat_postMessage.side_effect = Exception("network error")

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.send(self._make_outbox_item(), creds=_creds())

        assert result.success is False
        assert result.error is not None
        assert "network error" in result.error


class TestSlackPlatformEnrich:
    """enrich resolves user_name from users_info and caches the result."""

    def _make_inbound(self, user_id: str = "U1", user_name: str = "U1") -> object:
        from surogates.channels.inbound import InboundMessage
        return InboundMessage(
            kind="text",
            identifier="C123",
            thread_key=None,
            platform_user_id=user_id,
            user_name=user_name,
            text="hello",
            media_urls=[],
            media_types=[],
            is_dm=False,
            is_mention=False,
            ts="1700000001.000001",
            source={},
        )

    @pytest.mark.asyncio
    async def test_enrich_resolves_display_name(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.users_info.return_value = {
            "user": {
                "profile": {"display_name": "Alice", "real_name": "Alice Smith"},
                "name": "alice_slack",
            }
        }

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.enrich(self._make_inbound("U1", "U1"), creds=_creds())

        assert result.user_name == "Alice"

    @pytest.mark.asyncio
    async def test_enrich_falls_back_to_real_name(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.users_info.return_value = {
            "user": {
                "profile": {"display_name": "", "real_name": "Bob Jones"},
                "name": "bobjones",
            }
        }

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.enrich(self._make_inbound("U2", "U2"), creds=_creds())

        assert result.user_name == "Bob Jones"

    @pytest.mark.asyncio
    async def test_enrich_falls_back_to_slack_name(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.users_info.return_value = {
            "user": {
                "profile": {"display_name": "", "real_name": ""},
                "name": "charlie_s",
            }
        }

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.enrich(self._make_inbound("U3", "U3"), creds=_creds())

        assert result.user_name == "charlie_s"

    @pytest.mark.asyncio
    async def test_enrich_falls_back_to_user_id_on_error(self):
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.users_info.side_effect = Exception("api error")

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.enrich(self._make_inbound("U4", "U4"), creds=_creds())

        assert result.user_name == "U4"


    @pytest.mark.asyncio
    async def test_enrich_caches_users_info_per_token_and_user(self):
        """users_info is called once per (token, user_id), not once per enrich call."""
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.users_info.return_value = {
            "user": {
                "profile": {"display_name": "Alice", "real_name": ""},
                "name": "alice",
            }
        }

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            msg = self._make_inbound("U1", "U1")
            for _ in range(3):
                await p.enrich(msg, creds=_creds())

        assert mock_client.users_info.call_count == 1


def _make_slash_form(
    *,
    app_id: str = APP_ID,
    text: str = "hello world",
    channel_id: str = "D123",
    user_id: str = "U42",
    team_id: str = "T999",
    command: str = "/surogates",
) -> dict[str, str]:
    """Build a form dict that mirrors what Slack sends for a slash command."""
    return {
        "command": command,
        "text": text,
        "channel_id": channel_id,
        "user_id": user_id,
        "team_id": team_id,
        "api_app_id": app_id,
    }


def _make_interact_form(
    *,
    action_id: str = "surogates_approve_once",
) -> dict[str, str]:
    """Build a form dict that mirrors what Slack sends for a block_actions payload."""
    import json as _json
    payload = {
        "type": "block_actions",
        "actions": [{"action_id": action_id}],
    }
    return {"payload": _json.dumps(payload)}


class TestHandleInteractiveSlash:
    """handle_interactive on the /commands path produces a synthetic InboundMessage."""

    @pytest.mark.asyncio
    async def test_slash_with_text_returns_inbound_message(self):
        p = SlackPlatform()
        form = _make_slash_form(text="ask me something")
        from types import SimpleNamespace
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        result = await p.handle_interactive(
            "/slack/{app_id}/commands",
            form,
            request=request,
            creds=_creds(),
            routing=None,
        )
        from surogates.channels.inbound import InboundMessage
        assert isinstance(result, InboundMessage)
        assert result.text == "ask me something"


    @pytest.mark.asyncio
    async def test_slash_empty_text_returns_plain_text_response(self):
        """Empty slash text → response with usage hint; NOT an InboundMessage."""
        p = SlackPlatform()
        form = _make_slash_form(text="")
        from types import SimpleNamespace
        from fastapi.responses import Response
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        result = await p.handle_interactive(
            "/slack/{app_id}/commands",
            form,
            request=request,
            creds=_creds(),
            routing=None,
        )
        from surogates.channels.inbound import InboundMessage
        assert not isinstance(result, InboundMessage), (
            "Empty slash text must not produce an InboundMessage"
        )
        assert isinstance(result, Response)
        # Response body should contain usage guidance.
        body = result.body if hasattr(result, "body") else b""
        assert b"Usage" in body or b"usage" in body or b"surogates" in body.lower()

    @pytest.mark.asyncio
    async def test_slash_whitespace_only_text_returns_usage_response(self):
        """Whitespace-only text is treated as empty."""
        p = SlackPlatform()
        form = _make_slash_form(text="   ")
        from types import SimpleNamespace
        from fastapi.responses import Response
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        result = await p.handle_interactive(
            "/slack/{app_id}/commands",
            form,
            request=request,
            creds=_creds(),
            routing=None,
        )
        from surogates.channels.inbound import InboundMessage
        assert not isinstance(result, InboundMessage)
        assert isinstance(result, Response)


class TestHandleInteractiveInteract:
    """handle_interactive on the /interact path acks 200 (no pipeline invocation)."""

    @pytest.mark.asyncio
    async def test_interact_returns_response_not_inbound_message(self):
        """Block-actions payload → a Response, not an InboundMessage."""
        p = SlackPlatform()
        form = _make_interact_form()
        from types import SimpleNamespace
        from fastapi.responses import Response
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        result = await p.handle_interactive(
            "/slack/{app_id}/interact",
            form,
            request=request,
            creds=_creds(),
            routing=None,
        )
        from surogates.channels.inbound import InboundMessage
        assert not isinstance(result, InboundMessage)
        assert isinstance(result, Response)
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_interact_bad_payload_json_still_returns_200(self):
        """Malformed payload JSON on /interact → 200 (ack, don't crash)."""
        p = SlackPlatform()
        form = {"payload": "not valid json {{{{"}
        from types import SimpleNamespace
        from fastapi.responses import Response
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        result = await p.handle_interactive(
            "/slack/{app_id}/interact",
            form,
            request=request,
            creds=_creds(),
            routing=None,
        )
        assert isinstance(result, Response)
        assert result.status_code == 200


class TestSlackSendPrivate:
    """send_private opens a DM with the sender and posts there."""

    @pytest.mark.asyncio
    async def test_dms_the_sender(self):
        calls = {}

        class _FakeClient:
            async def conversations_open(self, users):
                calls["users"] = users
                return {"channel": {"id": "D123"}}

            async def chat_postMessage(self, **kw):
                calls["post"] = kw
                return {"ok": True, "ts": "1.0"}

        plat = SlackPlatform()
        plat._get_client = lambda token: _FakeClient()
        ok = await plat.send_private(
            {"bot_token": "xoxb"}, sender_id="U9", chat_id="C1", is_dm=False, text="link me",
        )
        assert ok is True
        assert calls["users"] == "U9"
        assert calls["post"]["channel"] == "D123"
        assert "link me" in calls["post"]["text"]

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self):
        class _FailClient:
            async def conversations_open(self, users):
                raise RuntimeError("cannot dm")

        plat = SlackPlatform()
        plat._get_client = lambda token: _FailClient()
        ok = await plat.send_private(
            {"bot_token": "x"}, sender_id="U9", chat_id="C1", is_dm=False, text="x",
        )
        assert ok is False


@pytest.mark.asyncio
async def test_slash_command_visibility_is_dm():
    p = SlackPlatform()
    form = _make_slash_form(text="hello", channel_id="D123")
    request = SimpleNamespace(path_params={"app_id": APP_ID})

    result = await p.handle_interactive(
        "/slack/{app_id}/commands",
        form,
        request=request,
        creds=_creds(),
        routing=None,
    )

    from surogates.channels.inbound import InboundMessage

    assert isinstance(result, InboundMessage)
    assert result.visibility == "dm"
