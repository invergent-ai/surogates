"""Tests for surogates.channels.platforms.slack — parse + verify + identifier_of.

TDD: tests written BEFORE the implementation module exists.  All tests in this
file must fail with ImportError or AttributeError until
``surogates/channels/platforms/slack.py`` is created.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

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
    identifier_of,
    parse,
    verify,
)
from surogates.channels.registry import VerificationResult  # noqa: E402


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
