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
        '{"token": "<token>", "password": "[REDACTED]"}',
        "export GITHUB_TOKEN=$GITHUB_TOKEN",
        "Authorization: Bearer {api_key}",
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
        # glued or plural names, and passwords with brackets: masked as before
        "PGPASSWORD=Xq7rT2mLp9sKd4": "Xq7rT2mLp9sKd4",
        "SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_GITHUB_CLIENT_SECRET=a1b2c3d4e5f6g7h8i9j0": "a1b2c3d4e5f6g7h8i9j0",
        "TOKEN_A_B_C_D_E_F_G_H_I_J=a1b2c3d4e5f6g7h8i9j0": "a1b2c3d4e5f6g7h8i9j0",
        "PASSWORD=Tr0ub4dor3()": "Tr0ub4dor3",
        "API_TOKENS=12345678901234567890": "12345678901234567890",
        "OTP_TOKENS=482913": "482913",
        "SECRET=Zx9Yw8['ab']": "Zx9Yw8",
        "NGROK_AUTHTOKEN=2aBcD3eFgH4iJkL5mNoP6qRs": "2aBcD3eFgH4iJkL5mNoP6qRs",
        "SECRETKEY=Zx9Yw8Vu7Ts6Rq5Po4": "Zx9Yw8Vu7Ts6Rq5Po4",
        "AUTHENTICATION_KEY=a1b2c3d4e5f6g7h8i9j0": "a1b2c3d4e5f6g7h8i9j0",
        "API_KEYS=k1a2b3c4d5e6f7g8h9,k9z8y7x6w5v4u3t2": "k1a2b3c4d5e6f7g8h9",
        "VALID_TOKENS=a1b2c3d4e5f6g7h8i9j0": "a1b2c3d4e5f6g7h8i9j0",
        "DB_PASSWORD=Xk9(mP2!vL": "Xk9(mP2!vL",
        "ADMIN_PASSWORD=Summer[2024]!": "Summer[2024]",
        "PASSWORD=(Tr0ub4dor3)": "Tr0ub4dor3",
        'DOCKER_AUTH_CONFIG={"auths":{"r.test":{"auth":"dXNlcjpzM2NyM3RQYXNz"}}}': "dXNlcjpzM2NyM3RQYXNz",
        # letters-only or $-prefixed secrets are not placeholders
        "API_KEY=<CorrectHorseBatteryStaple>": "CorrectHorseBatteryStaple",
        "PASSWORD=your-horse-battery-staple": "horse-battery-staple",
        "PASSWORD=$UPERSECRETPASSWORD": "UPERSECRETPASSWORD",
        '{"password": "[opensesame]"}': "opensesame",
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
        "MAX_NEW_TOKENS=512",
        "TOKENIZER=cl100k_base",
        'API_KEY = os.getenv("API_KEY")',
        "TOKEN = settings['api_token']",
        "GITHUB_TOKEN = get_token()",
    ):
        assert redact_sensitive_data(text, phones=False) == text, text


def test_long_inputs_redact_in_linear_time() -> None:
    import time

    for text in (
        "Authorization: Bearer a" + "." * 50_000 + "x",
        "TOKEN_" * 8_000 + "X",
        "A_" * 25_000 + "TOKEN_X",
        "TOKEN=" + "a_" * 50_000,
    ):
        started = time.perf_counter()
        redact_sensitive_data(text, phones=False)
        assert time.perf_counter() - started < 0.5
