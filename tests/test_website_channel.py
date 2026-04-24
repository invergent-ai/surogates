"""Unit tests for the website-channel helpers.

Exercises the pieces that don't need a running database: publishable
key generation/hash/recognition, origin allow-list normalisation, CSRF
token verify, and the website-session JWT encode/decode cycle.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("SUROGATES_JWT_SECRET", "website-channel-test-secret")

from surogates.channels.website_agent_store import (
    PUBLISHABLE_KEY_PREFIX,
    generate_publishable_key,
    hash_publishable_key,
    is_publishable_key,
    origin_allowed,
)
from surogates.channels.website_session import (
    create_website_session_token,
    decode_website_session_token,
    generate_csrf_token,
    verify_csrf_token,
)
from surogates.tenant.auth.jwt import InvalidTokenError, create_access_token


# ---------------------------------------------------------------------------
# Publishable key format
# ---------------------------------------------------------------------------


class TestPublishableKey:
    def test_generate_is_prefixed_and_unique(self):
        k1 = generate_publishable_key()
        k2 = generate_publishable_key()
        assert k1.startswith(PUBLISHABLE_KEY_PREFIX)
        assert k2.startswith(PUBLISHABLE_KEY_PREFIX)
        assert k1 != k2

    def test_generate_has_sufficient_entropy(self):
        """264 bits of entropy means the base64url body is ~44 chars."""
        k = generate_publishable_key()
        suffix = k[len(PUBLISHABLE_KEY_PREFIX):]
        assert len(suffix) >= 40, "publishable key should have ~264 bits of entropy"

    def test_hash_is_deterministic_and_hex(self):
        k = generate_publishable_key()
        assert hash_publishable_key(k) == hash_publishable_key(k)
        assert len(hash_publishable_key(k)) == 64  # SHA-256 hex

    def test_hash_different_for_different_keys(self):
        assert hash_publishable_key("a") != hash_publishable_key("b")

    def test_is_publishable_key_discriminates(self):
        assert is_publishable_key(generate_publishable_key()) is True
        assert is_publishable_key("surg_sk_something") is False
        assert is_publishable_key("") is False
        assert is_publishable_key("random-text") is False


# ---------------------------------------------------------------------------
# Origin allow-list
# ---------------------------------------------------------------------------


class TestOriginAllowed:
    def test_exact_match_allowed(self):
        assert origin_allowed("https://customer.com", ("https://customer.com",))

    def test_normalises_case_and_trailing_slash(self):
        """Browsers send origin in lowercase without a slash; configs drift."""
        allowed = ("https://customer.com",)
        assert origin_allowed("HTTPS://CUSTOMER.COM", allowed)
        assert origin_allowed("https://customer.com/", allowed)
        assert origin_allowed("HTTPS://CUSTOMER.COM/", allowed)

    def test_different_host_rejected(self):
        assert not origin_allowed("https://evil.com", ("https://customer.com",))

    def test_different_port_rejected(self):
        """Port is part of the origin -- 80 and 8080 are distinct."""
        assert not origin_allowed(
            "https://customer.com:8080", ("https://customer.com",),
        )

    def test_different_scheme_rejected(self):
        assert not origin_allowed(
            "http://customer.com", ("https://customer.com",),
        )

    def test_wildcards_not_supported(self):
        """No wildcards -- ops must enumerate origins explicitly."""
        assert not origin_allowed("https://app.customer.com", ("https://*.customer.com",))

    def test_missing_origin_rejected(self):
        assert not origin_allowed(None, ("https://customer.com",))
        assert not origin_allowed("", ("https://customer.com",))

    def test_empty_allow_list_rejects_everything(self):
        assert not origin_allowed("https://customer.com", ())


# ---------------------------------------------------------------------------
# CSRF double-submit
# ---------------------------------------------------------------------------


class TestCsrfTokens:
    def test_generate_is_url_safe_and_unique(self):
        a = generate_csrf_token()
        b = generate_csrf_token()
        assert a != b
        assert len(a) >= 40  # 256 bits → ~43 chars base64url

    def test_verify_match(self):
        tok = generate_csrf_token()
        assert verify_csrf_token(tok, tok) is True

    def test_verify_mismatch(self):
        a = generate_csrf_token()
        b = generate_csrf_token()
        assert verify_csrf_token(a, b) is False

    def test_verify_missing_sides_are_always_false(self):
        """A missing header is the definition of a CSRF attack attempt."""
        assert verify_csrf_token("cookie-token", None) is False
        assert verify_csrf_token(None, "header-token") is False
        assert verify_csrf_token(None, None) is False
        assert verify_csrf_token("", "") is False


# ---------------------------------------------------------------------------
# Website-session JWT
# ---------------------------------------------------------------------------


class TestWebsiteSessionToken:
    @pytest.fixture
    def claims_input(self):
        return {
            "session_id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "agent_id": uuid.uuid4(),
            "origin": "https://customer.com",
            "csrf_token": generate_csrf_token(),
        }

    def test_encode_decode_roundtrip(self, claims_input):
        token = create_website_session_token(**claims_input)
        decoded = decode_website_session_token(token)
        assert decoded.session_id == claims_input["session_id"]
        assert decoded.org_id == claims_input["org_id"]
        assert decoded.agent_id == claims_input["agent_id"]
        assert decoded.origin == claims_input["origin"]
        assert decoded.csrf_token == claims_input["csrf_token"]
        assert decoded.expires_at > decoded.issued_at

    def test_access_token_rejected_as_website_session(self, claims_input):
        """A valid access JWT must not authenticate a website session."""
        access = create_access_token(
            claims_input["org_id"], uuid.uuid4(), {"sessions:read"},
        )
        with pytest.raises(InvalidTokenError):
            decode_website_session_token(access)

    def test_tampered_signature_rejected(self, claims_input):
        token = create_website_session_token(**claims_input)
        # Flip a character in the signature half of the JWT.
        head, body, sig = token.rsplit(".", 2)
        tampered = f"{head}.{body}.{sig[:-2]}AA"
        with pytest.raises(InvalidTokenError):
            decode_website_session_token(tampered)

    def test_malformed_token_rejected(self):
        with pytest.raises(InvalidTokenError):
            decode_website_session_token("not-a-jwt")

    def test_expired_token_rejected(self, claims_input):
        token = create_website_session_token(
            **claims_input, expires_seconds=-1,
        )
        with pytest.raises(InvalidTokenError):
            decode_website_session_token(token)
