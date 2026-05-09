"""Secret redaction tests for logs and persisted session events."""

from __future__ import annotations

import json
import logging

from surogates.harness.redact import redact_sensitive_data, redact_sensitive_text
from surogates.logging_config import StructuredFormatter


def test_redacts_common_secret_text_patterns() -> None:
    raw = "\n".join(
        [
            "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456",
            "https://api.example.test/callback?code=oauth-code&state=ok&access_token=opaque-token",
            "https://user:password123@example.test/path",
            "postgresql://app:db-password@example.test:5432/app",
            "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature",
            "-----BEGIN PRIVATE KEY-----\nsecret-key-material\n-----END PRIVATE KEY-----",
        ]
    )

    redacted = redact_sensitive_text(raw)

    assert "abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in redacted
    assert "oauth-code" not in redacted
    assert "opaque-token" not in redacted
    assert "password123" not in redacted
    assert "db-password" not in redacted
    assert "payload.signature" not in redacted
    assert "secret-key-material" not in redacted
    assert "OPENAI_API_KEY=" in redacted
    assert "Authorization: Bearer " in redacted
    assert "code=***" in redacted
    assert "access_token=***" in redacted
    assert "[REDACTED PRIVATE KEY]" in redacted


def test_redacts_nested_event_payload_without_mutating_input() -> None:
    payload = {
        "content": "use sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "metadata": {
            "url": "https://example.test/path?api_key=super-secret&safe=yes",
            "items": ["TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456"],
        },
        "count": 3,
        "safe": True,
    }

    redacted = redact_sensitive_data(payload)

    assert payload["content"].endswith("abcdefghijklmnopqrstuvwxyz123456")
    serialized = json.dumps(redacted)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in serialized
    assert "super-secret" not in serialized
    assert '"count": 3' in serialized
    assert '"safe": true' in serialized


def test_structured_formatter_redacts_message_and_exception() -> None:
    formatter = StructuredFormatter()
    record = logging.LogRecord(
        name="surogates.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed with token %s",
        args=("sk-proj-abcdefghijklmnopqrstuvwxyz123456",),
        exc_info=None,
    )
    record.trace_id = ""
    record.span_id = ""
    record.parent_span_id = ""

    rendered = formatter.format(record)

    assert "abcdefghijklmnopqrstuvwxyz123456" not in rendered
    assert "sk-pro" in rendered
    assert "***" in rendered or "..." in rendered
