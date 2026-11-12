"""Redaction masks secrets, not the meaning of what was said.

The session's events are the conversation the user reads back and the text
the model's context is rebuilt from, so a mask there must hide a secret and
nothing else.
"""

from __future__ import annotations

import json
import logging

from surogates.harness.redact import redact_sensitive_data, redact_sensitive_text
from surogates.logging_config import StructuredFormatter

PHONE = "+919876543210"


def test_an_events_phone_number_is_kept_as_written() -> None:
    event = {"content": f"Add the new hire with phone {PHONE}.", "args": {"phone": PHONE}}

    stored = redact_sensitive_data(event, phones=False)

    assert stored == event


def test_a_log_line_still_masks_a_phone_number() -> None:
    record = logging.LogRecord(
        name="surogates.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="calling %s", args=(PHONE,), exc_info=None,
    )
    record.trace_id = record.span_id = record.parent_span_id = ""

    rendered = StructuredFormatter().format(record)

    assert PHONE not in rendered
    assert "+919****3210" in rendered
    assert redact_sensitive_text(f"call {PHONE}") == "call +919****3210"


def test_a_placeholder_where_a_token_would_go_is_kept() -> None:
    for text in (
        "curl -H 'Authorization: Bearer <jwt>').",
        "Send `Authorization: Bearer <access token>` with every call.",
        "Authorization: Bearer $TOKEN",
        "Authorization: Bearer ${API_TOKEN}",
        "Authorization: Bearer [REDACTED]",
        "export OPENAI_API_KEY=<your-key>",
        "OPENAI_API_KEY=YOUR_API_KEY",
        "TOKEN=${TOKEN}",
        '{"token": "<token>", "password": "xxxxxx"}',
    ):
        assert redact_sensitive_data(text, phones=False) == text, text


def test_the_punctuation_after_a_token_is_kept() -> None:
    redacted = redact_sensitive_text("Use (Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123).")

    assert redacted == "Use (Authorization: Bearer abcdef...0123)."


def test_real_secrets_are_still_masked_in_events() -> None:
    secrets = {
        "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456": "abcdefghijklmnopqrstuvwxyz123456",
        "Authorization: Bearer abc123": "abc123",
        "Authorization: Bearer $ecret123": "$ecret123",
        "Authorization: Bearer a8F3kq9Zx2Lm7Qp4Rt6Vw1Yz": "a8F3kq9Zx2Lm7Qp4Rt6Vw1Yz",
        "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456": "abcdefghijklmnopqrstuvwxyz123456",
        "DB_PASSWORD=my_dog_rex": "my_dog_rex",
        "API_KEY=<a1b2c3d4e5f6>": "a1b2c3d4e5f6",
        '{"token": "abc123def456ghi789xyz"}': "def456ghi789",
        "https://api.example.test/cb?code=oauth-code&access_token=opaque-token": "opaque-token",
        "postgresql://app:db-password@example.test:5432/app": "db-password",
        "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature": "payload.signature",
    }
    for text, secret in secrets.items():
        stored = json.dumps(redact_sensitive_data({"content": text}, phones=False))
        assert secret not in stored, text


def test_numbers_and_code_are_not_read_as_phones_or_secrets_in_events() -> None:
    for text in (
        "Sum check: 707875+1671239582+1203+994582 = 3,383,242",
        "| 1944 | 41684987 | +1561235477 | 40119510 |",
        "Billed_Revenue_Cents: 18500000+18750000+19000000",
        "AUTHORITY_RANK = {'Head office': 30, 'Service lead': 20}",
        "MAX_TOKENS = 4096",
        'API_KEY = os.getenv("API_KEY")',
    ):
        assert redact_sensitive_data(text, phones=False) == text, text
