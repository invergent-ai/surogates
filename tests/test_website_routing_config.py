"""Per-agent website routing config: origins/cap helpers and the
``channel_identifier`` cookie claim."""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("SUROGATES_AUTH_JWT_SECRET", "test-secret-key-for-tests-0123456789")

from surogates.api.routes.website import (
    _agent_allowed_origins,
    _routing_channel_config,
)
from surogates.channels.website_session import (
    create_website_session_token,
    decode_website_session_token,
)


class TestRoutingChannelConfig:
    def test_none_routing_is_empty(self):
        assert _routing_channel_config(None) == {}

    def test_non_dict_config_is_empty(self):
        assert _routing_channel_config({"config": "oops"}) == {}

    def test_dict_config_passes_through(self):
        assert _routing_channel_config({"config": {"a": 1}}) == {"a": 1}


class TestAgentAllowedOrigins:
    def test_absent_returns_none_for_global_fallback(self):
        assert _agent_allowed_origins({}) is None

    def test_list_is_normalized(self):
        origins = _agent_allowed_origins(
            {"allowed_origins": ["HTTPS://Customer.com/", "https://b.io"]}
        )
        assert origins == ("https://customer.com", "https://b.io")

    def test_csv_string_accepted(self):
        origins = _agent_allowed_origins({"allowed_origins": "https://a.com, https://b.io"})
        assert origins == ("https://a.com", "https://b.io")

    def test_empty_list_returns_none(self):
        assert _agent_allowed_origins({"allowed_origins": []}) is None


class TestChannelIdentifierClaim:
    def test_roundtrip(self):
        sid, org = uuid.uuid4(), uuid.uuid4()
        token = create_website_session_token(
            session_id=sid,
            org_id=org,
            origin="https://a.com",
            csrf_token="csrf",
            channel_identifier="surg_wk_abc",
        )
        claims = decode_website_session_token(token)
        assert claims.channel_identifier == "surg_wk_abc"

    def test_legacy_token_without_claim_decodes_empty(self):
        sid, org = uuid.uuid4(), uuid.uuid4()
        token = create_website_session_token(
            session_id=sid, org_id=org, origin="https://a.com", csrf_token="c",
        )
        claims = decode_website_session_token(token)
        assert claims.channel_identifier == ""
