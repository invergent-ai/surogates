"""Tests for surogates.channels.platforms.slack — parse + verify + identifier_of
+ SlackPlatform (the registered channel platform object).

TDD: tests written BEFORE the implementation module exists.  All tests in this
file must fail with ImportError or AttributeError until
``surogates/channels/platforms/slack.py`` is created.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — shared across tests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Import under test — will fail until implementation exists
# ---------------------------------------------------------------------------

from surogates.channels.platforms.slack import (  # noqa: E402
    SlackPlatform,
    identifier_of,
    parse,
    verify,
)
from surogates.channels.registry import ChannelRegistry, VerificationResult  # noqa: E402


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


class TestIdentifierOf:
    def test_reads_app_id_from_path_params(self):
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        assert identifier_of(request, {}) == APP_ID

    def test_ignores_body_entirely(self):
        """body may be empty dict (url_verification has no api_app_id)."""
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        assert identifier_of(request, {}) == APP_ID
        assert identifier_of(request, None) == APP_ID


# ---------------------------------------------------------------------------
# verify — signature checks
# ---------------------------------------------------------------------------


class TestVerifySignature:
    def test_accepts_correctly_signed_body(self):
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        request = _make_request(raw_body=raw_body)
        result = verify(request, raw_body, creds=_creds())
        # For a plain event_callback the result should be True (bool)
        assert result is True

    def test_rejects_tampered_body(self):
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        tampered = raw_body + b" tampered"
        # Signature computed over original body, but body passed is tampered.
        ts = str(int(time.time()))
        sig = _slack_signature(SIGNING_SECRET, ts, raw_body)
        request = SimpleNamespace(
            path_params={"app_id": APP_ID},
            headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
            },
        )
        result = verify(request, tampered, creds=_creds())
        assert result is False

    def test_rejects_stale_timestamp(self):
        """Timestamps more than 5 minutes old must be rejected."""
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        stale_ts = str(int(time.time()) - 310)  # 5 min + 10 s ago
        sig = _slack_signature(SIGNING_SECRET, stale_ts, raw_body)
        request = SimpleNamespace(
            path_params={"app_id": APP_ID},
            headers={
                "x-slack-request-timestamp": stale_ts,
                "x-slack-signature": sig,
            },
        )
        result = verify(request, raw_body, creds=_creds())
        assert result is False

    def test_rejects_future_timestamp(self):
        """Timestamps more than 5 minutes in the future must also be rejected."""
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        future_ts = str(int(time.time()) + 310)
        sig = _slack_signature(SIGNING_SECRET, future_ts, raw_body)
        request = SimpleNamespace(
            path_params={"app_id": APP_ID},
            headers={
                "x-slack-request-timestamp": future_ts,
                "x-slack-signature": sig,
            },
        )
        result = verify(request, raw_body, creds=_creds())
        assert result is False

    def test_rejects_wrong_signing_secret(self):
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        request = _make_request(raw_body=raw_body, signing_secret="wrong_secret")
        # The creds use a different secret so the signature won't match.
        ts = str(int(time.time()))
        sig = _slack_signature("wrong_secret", ts, raw_body)
        request = SimpleNamespace(
            path_params={"app_id": APP_ID},
            headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": sig,
            },
        )
        result = verify(request, raw_body, creds=_creds(signing_secret=SIGNING_SECRET))
        assert result is False


# ---------------------------------------------------------------------------
# verify — unconfigured credential (must reject cleanly, never crash)
# ---------------------------------------------------------------------------


class TestVerifyUnconfiguredCredential:
    def test_none_signing_secret_returns_false_no_exception(self):
        """A vault entry of {"signing_secret": None} must reject, not crash."""
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        request = _make_request(raw_body=raw_body)
        result = verify(request, raw_body, creds={"signing_secret": None})
        assert result is False

    def test_missing_signing_secret_returns_false_no_exception(self):
        """Empty creds (no signing_secret key) must reject, not crash."""
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        request = _make_request(raw_body=raw_body)
        result = verify(request, raw_body, creds={})
        assert result is False

    def test_empty_string_signing_secret_returns_false(self):
        raw_body = b'{"type":"event_callback","api_app_id":"A0TESTAPPID"}'
        request = _make_request(raw_body=raw_body)
        result = verify(request, raw_body, creds={"signing_secret": ""})
        assert result is False


# ---------------------------------------------------------------------------
# verify — url_verification handshake
# ---------------------------------------------------------------------------


class TestVerifyUrlVerification:
    def test_url_verification_returns_verification_result_with_challenge(self):
        """Slack sends a url_verification challenge — we must echo it back."""
        challenge = "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXUpGGS"
        body_dict = {"type": "url_verification", "challenge": challenge}
        import json
        raw_body = json.dumps(body_dict).encode()
        request = _make_request(raw_body=raw_body)

        result = verify(request, raw_body, creds=_creds())

        assert isinstance(result, VerificationResult)
        assert result.accepted is True
        assert result.status_code == 200
        # response_body should carry the challenge (dict or str containing challenge)
        if isinstance(result.response_body, dict):
            assert result.response_body.get("challenge") == challenge
        else:
            assert challenge in str(result.response_body)

    def test_url_verification_bad_signature_rejected(self):
        """Even for url_verification, the signature must be valid."""
        import json
        challenge = "somechallenge"
        body_dict = {"type": "url_verification", "challenge": challenge}
        raw_body = json.dumps(body_dict).encode()

        ts = str(int(time.time()))
        # Use wrong signing secret to produce bad signature.
        bad_sig = _slack_signature("bad_secret", ts, raw_body)
        request = SimpleNamespace(
            path_params={"app_id": APP_ID},
            headers={
                "x-slack-request-timestamp": ts,
                "x-slack-signature": bad_sig,
            },
        )
        result = verify(request, raw_body, creds=_creds())
        assert result is False


# ---------------------------------------------------------------------------
# verify — api_app_id cross-check
# ---------------------------------------------------------------------------


class TestVerifyAppIdCrossCheck:
    def test_event_callback_with_mismatched_api_app_id_rejected(self):
        """A signed request where api_app_id != path app_id must be rejected."""
        import json
        body_dict = {
            "type": "event_callback",
            "api_app_id": "ADIFFERENTAPP",
            "event": {"type": "message", "text": "hello", "user": "U1"},
        }
        raw_body = json.dumps(body_dict).encode()
        request = _make_request(raw_body=raw_body)  # path has APP_ID

        result = verify(request, raw_body, creds=_creds())
        assert result is False

    def test_event_callback_with_matching_api_app_id_accepted(self):
        import json
        body_dict = {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {"type": "message", "text": "hello", "user": "U1"},
        }
        raw_body = json.dumps(body_dict).encode()
        request = _make_request(raw_body=raw_body)

        result = verify(request, raw_body, creds=_creds())
        assert result is True


# ---------------------------------------------------------------------------
# parse — message event mapping
# ---------------------------------------------------------------------------


class TestParsePlainMessage:
    """parse() maps event_callback/message to InboundMessage."""

    def _make_event_callback(self, event: dict, api_app_id: str = APP_ID) -> dict:
        return {
            "type": "event_callback",
            "api_app_id": api_app_id,
            "event": event,
        }

    def test_dm_message_is_dm_true_is_mention_false(self):
        body = self._make_event_callback({
            "type": "message",
            "text": "hello bot",
            "user": "U1",
            "channel": "D123",
            "channel_type": "im",
            "ts": "1700000001.000100",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.is_dm is True
        assert result.is_mention is False
        assert result.text == "hello bot"
        assert result.platform_user_id == "U1"
        assert result.identifier == "D123"
        assert result.ts == "1700000001.000100"
        assert result.thread_key is None  # DM without thread_ts

    def test_channel_message_with_mention_is_mention_true(self):
        body = self._make_event_callback({
            "type": "message",
            "text": f"<@{BOT_USER_ID}> please help",
            "user": "U2",
            "channel": "C456",
            "channel_type": "channel",
            "ts": "1700000002.000200",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.is_dm is False
        assert result.is_mention is True
        assert result.identifier == "C456"

    def test_channel_message_without_mention_is_mention_false(self):
        body = self._make_event_callback({
            "type": "message",
            "text": "just chatting",
            "user": "U2",
            "channel": "C456",
            "channel_type": "channel",
            "ts": "1700000003.000300",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.is_mention is False
        assert result.is_dm is False

    def test_thread_key_set_from_thread_ts_for_channel(self):
        body = self._make_event_callback({
            "type": "message",
            "text": "thread reply",
            "user": "U3",
            "channel": "C789",
            "channel_type": "channel",
            "ts": "1700000010.000500",
            "thread_ts": "1700000001.000001",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.thread_key == "1700000001.000001"

    def test_thread_key_is_ts_for_root_channel_message(self):
        """Root channel message has no thread_ts — thread_key falls back to ts (the root)."""
        body = self._make_event_callback({
            "type": "message",
            "text": "root message",
            "user": "U3",
            "channel": "C789",
            "channel_type": "channel",
            "ts": "1700000020.000600",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        # Root channel message: thread_key = ts (same as slack.py behavior)
        assert result.thread_key == "1700000020.000600"

    def test_dm_thread_key_is_thread_ts_when_present(self):
        body = self._make_event_callback({
            "type": "message",
            "text": "threaded dm",
            "user": "U4",
            "channel": "D999",
            "channel_type": "im",
            "ts": "1700000030.000700",
            "thread_ts": "1700000025.000001",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.is_dm is True
        assert result.thread_key == "1700000025.000001"

    def test_dm_thread_key_is_none_when_no_thread_ts(self):
        body = self._make_event_callback({
            "type": "message",
            "text": "plain dm",
            "user": "U4",
            "channel": "D999",
            "channel_type": "im",
            "ts": "1700000040.000800",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.thread_key is None

    def test_identifier_is_channel_id(self):
        """InboundMessage.identifier should be the Slack channel id (the chat)."""
        body = self._make_event_callback({
            "type": "message",
            "text": "hi",
            "user": "U5",
            "channel": "C111",
            "channel_type": "channel",
            "ts": "1700000050.000900",
        })

        result = parse(body, bot_user_id=BOT_USER_ID)

        # identifier in InboundMessage is the platform chat/channel id (channel field)
        assert result is not None
        assert result.identifier == "C111"


# ---------------------------------------------------------------------------
# parse — app_mention event
# ---------------------------------------------------------------------------


class TestParseAppMention:
    def test_app_mention_maps_to_inbound_message_with_is_mention_true(self):
        body = {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "app_mention",
                "text": f"<@{BOT_USER_ID}> do this",
                "user": "U6",
                "channel": "C222",
                "ts": "1700000060.001000",
            },
        }

        result = parse(body, bot_user_id=BOT_USER_ID)

        assert result is not None
        assert result.is_mention is True
        assert result.is_dm is False
        assert result.platform_user_id == "U6"


# ---------------------------------------------------------------------------
# parse — None returns (bot messages, edits, deletions, non-message types)
# ---------------------------------------------------------------------------


class TestParseReturnsNone:
    def _evt(self, **kw) -> dict:
        return {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "channel_type": "channel",
                "ts": "1700000100.000001",
                **kw,
            },
        }

    def test_bot_message_returns_none(self):
        body = self._evt(bot_id="B123", text="I am a bot", user="")
        assert parse(body, bot_user_id=BOT_USER_ID) is None

    def test_bot_subtype_returns_none(self):
        body = self._evt(subtype="bot_message", text="bot says hi")
        assert parse(body, bot_user_id=BOT_USER_ID) is None

    def test_message_changed_returns_none(self):
        body = self._evt(subtype="message_changed", text="edited")
        assert parse(body, bot_user_id=BOT_USER_ID) is None

    def test_message_deleted_returns_none(self):
        body = self._evt(subtype="message_deleted", text="")
        assert parse(body, bot_user_id=BOT_USER_ID) is None

    def test_non_message_event_type_returns_none(self):
        body = {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "reaction_added",
                "user": "U1",
                "reaction": "thumbsup",
                "ts": "1700000200.000001",
            },
        }
        assert parse(body, bot_user_id=BOT_USER_ID) is None

    def test_url_verification_body_returns_none(self):
        """url_verification should not produce an InboundMessage."""
        body = {"type": "url_verification", "challenge": "abc123"}
        assert parse(body, bot_user_id=BOT_USER_ID) is None

    def test_missing_user_returns_none(self):
        body = self._evt(text="hello")
        body["event"].pop("user", None)
        assert parse(body, bot_user_id=BOT_USER_ID) is None


# ---------------------------------------------------------------------------
# parse — mpim (multi-party DM) is also is_dm=True
# ---------------------------------------------------------------------------


class TestParseMpim:
    def test_mpim_channel_type_is_dm_true(self):
        body = {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "message",
                "text": "group dm message",
                "user": "U7",
                "channel": "G333",
                "channel_type": "mpim",
                "ts": "1700000070.001100",
            },
        }
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        assert result.is_dm is True


# ---------------------------------------------------------------------------
# parse — files produce text marker + media_urls (no silent drop)
# ---------------------------------------------------------------------------


class TestParseFiles:
    """Files in the event are surfaced via a text marker and media_urls."""

    def _event_callback_with_files(self, files: list[dict]) -> dict:
        return {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "message",
                "text": "look at this",
                "user": "U1",
                "channel": "C123",
                "channel_type": "channel",
                "ts": "1700000080.000100",
                "files": files,
            },
        }

    def test_single_file_appends_marker_to_text(self):
        body = self._event_callback_with_files([
            {"name": "image.png", "url_private": "https://files.slack.com/img.png"},
        ])
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        assert "[shared 1 file(s):" in result.text
        assert "image.png" in result.text

    def test_single_file_populates_media_urls(self):
        body = self._event_callback_with_files([
            {"name": "image.png", "url_private": "https://files.slack.com/img.png"},
        ])
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        assert result.media_urls == ["https://files.slack.com/img.png"]

    def test_url_private_download_preferred_over_url_private(self):
        body = self._event_callback_with_files([
            {
                "name": "doc.pdf",
                "url_private_download": "https://files.slack.com/doc.pdf?dl=1",
                "url_private": "https://files.slack.com/doc.pdf",
            },
        ])
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        assert result.media_urls == ["https://files.slack.com/doc.pdf?dl=1"]

    def test_multiple_files_marker_and_all_urls(self):
        body = self._event_callback_with_files([
            {"name": "a.png", "url_private": "https://files.slack.com/a.png"},
            {"name": "b.txt", "url_private": "https://files.slack.com/b.txt"},
        ])
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        assert "[shared 2 file(s):" in result.text
        assert "a.png" in result.text
        assert "b.txt" in result.text
        assert len(result.media_urls) == 2

    def test_file_without_url_is_skipped(self):
        body = self._event_callback_with_files([
            {"name": "nope.bin"},  # no url_private / url_private_download
        ])
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        # No URL → no media entry; text unchanged (no marker for zero-url files)
        assert result.media_urls == []

    def test_no_files_no_marker(self):
        body = {
            "type": "event_callback",
            "api_app_id": APP_ID,
            "event": {
                "type": "message",
                "text": "plain text",
                "user": "U1",
                "channel": "C123",
                "channel_type": "channel",
                "ts": "1700000090.000100",
            },
        }
        result = parse(body, bot_user_id=BOT_USER_ID)
        assert result is not None
        assert "[shared" not in result.text
        assert result.media_urls == []


# ---------------------------------------------------------------------------
# SlackPlatform — route_path
# ---------------------------------------------------------------------------


class TestSlackPlatformRoutePath:
    def test_template_path_no_identifier(self):
        p = SlackPlatform()
        assert p.route_path() == "/slack/{app_id}"

    def test_concrete_path_with_identifier(self):
        p = SlackPlatform()
        assert p.route_path(APP_ID) == f"/slack/{APP_ID}"

    def test_concrete_path_none_matches_template(self):
        p = SlackPlatform()
        assert p.route_path(None) == "/slack/{app_id}"


# ---------------------------------------------------------------------------
# SlackPlatform — kind / topology / interactive_paths
# ---------------------------------------------------------------------------


class TestSlackPlatformMetadata:
    def test_kind_is_slack(self):
        assert SlackPlatform().kind == "slack"

    def test_topology_is_webhook(self):
        assert SlackPlatform().topology == "webhook"

    def test_interactive_paths_declared(self):
        p = SlackPlatform()
        paths = getattr(p, "interactive_paths", ())
        assert len(paths) == 2
        # Template form (path params, not concrete IDs).
        assert any("interact" in path for path in paths)
        assert any("commands" in path for path in paths)


# ---------------------------------------------------------------------------
# SlackPlatform — descriptor
# ---------------------------------------------------------------------------


class TestSlackPlatformDescriptor:
    def test_vault_refs_returns_bot_token_and_signing_secret(self):
        p = SlackPlatform()
        refs = p.descriptor.vault_refs(APP_ID)
        assert "bot_token" in refs
        assert "signing_secret" in refs

    def test_webhook_registration_is_manual(self):
        p = SlackPlatform()
        assert p.descriptor.webhook_registration == "manual"

    def test_config_keys_contains_required_keys(self):
        p = SlackPlatform()
        keys = p.descriptor.config_keys
        for expected in (
            "require_mention",
            "free_response_channels",
            "allow_bots",
            "reply_in_thread",
            "reply_broadcast",
        ):
            assert expected in keys, f"config_keys missing: {expected}"


# ---------------------------------------------------------------------------
# SlackPlatform — identifier_of / verify delegate to module functions
# ---------------------------------------------------------------------------


class TestSlackPlatformDelegation:
    def test_identifier_of_delegates_to_module_function(self):
        p = SlackPlatform()
        request = SimpleNamespace(path_params={"app_id": APP_ID})
        assert p.identifier_of(request, {}) == APP_ID

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


# ---------------------------------------------------------------------------
# SlackPlatform — parse (with creds; auth.test cached)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SlackPlatform — send
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SlackPlatform — enrich (user name resolution)
# ---------------------------------------------------------------------------


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
    async def test_enrich_returns_new_inbound_message_instance(self):
        """enrich must return a new InboundMessage (frozen dataclass)."""
        p = SlackPlatform()
        mock_client = AsyncMock()
        mock_client.users_info.return_value = {
            "user": {
                "profile": {"display_name": "Alice", "real_name": ""},
                "name": "alice",
            }
        }

        msg = self._make_inbound("U1", "U1")

        with patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
            return_value=mock_client,
        ):
            result = await p.enrich(msg, creds=_creds())

        assert result is not msg
        assert result.user_name == "Alice"

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


# ---------------------------------------------------------------------------
# SlackPlatform — registry self-registration
# ---------------------------------------------------------------------------


class TestSlackPlatformRegistration:
    def test_slack_registered_in_module_registry(self):
        """Importing platforms.slack registers 'slack' in the module-level registry."""
        import surogates.channels.platforms.slack  # noqa: F401 — trigger self-register
        from surogates.channels.registry import registry

        platform = registry.get("slack")
        assert platform is not None
        assert platform.kind == "slack"

    def test_registry_get_slack_returns_slack_platform_instance(self):
        import surogates.channels.platforms.slack  # noqa: F401
        from surogates.channels.registry import registry

        platform = registry.get("slack")
        assert isinstance(platform, SlackPlatform)


# ---------------------------------------------------------------------------
# SlackPlatform — handle_interactive (slash commands + interactivity)
# ---------------------------------------------------------------------------


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
    async def test_slash_message_is_dm(self):
        p = SlackPlatform()
        form = _make_slash_form(text="hello", channel_id="D123")
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
        assert result.is_dm is True

    @pytest.mark.asyncio
    async def test_slash_is_not_mention(self):
        p = SlackPlatform()
        form = _make_slash_form(text="hello")
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
        assert result.is_mention is False

    @pytest.mark.asyncio
    async def test_slash_identifier_is_channel_id(self):
        p = SlackPlatform()
        form = _make_slash_form(text="hello", channel_id="D999")
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
        assert result.identifier == "D999"

    @pytest.mark.asyncio
    async def test_slash_platform_user_id_is_user_id(self):
        p = SlackPlatform()
        form = _make_slash_form(text="hello", user_id="U77")
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
        assert result.platform_user_id == "U77"

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

    @pytest.mark.asyncio
    async def test_slash_ts_field_is_a_string(self):
        """ts must be a non-empty string (required by InboundMessage dedup)."""
        p = SlackPlatform()
        form = _make_slash_form(text="ping")
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
        assert isinstance(result.ts, str) and result.ts != ""


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
