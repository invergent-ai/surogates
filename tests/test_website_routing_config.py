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


class TestWebsiteEnabledDefault:
    """``channel_routing`` is the gate; the global flag must not be a
    second one that a deployment has to remember to set.

    A prod deployment whose runtime config carried no ``website:`` block
    404'd every ``/v1/website/*`` request while Studio showed the channel
    "Live on your site" — Studio reads the routing row it just wrote and
    cannot see this flag.
    """

    def test_defaults_on(self, monkeypatch):
        # load_settings() writes flattened YAML keys into os.environ
        # permanently, so a sibling test that loaded a config carrying
        # website.enabled=false would otherwise decide this one.
        monkeypatch.delenv("SUROGATES_WEBSITE_ENABLED", raising=False)
        from surogates.config import WebsiteSettings

        assert WebsiteSettings().enabled is True

    def test_env_can_still_turn_it_off(self, monkeypatch):
        from surogates.config import WebsiteSettings

        monkeypatch.setenv("SUROGATES_WEBSITE_ENABLED", "false")
        assert WebsiteSettings().enabled is False

    def test_absent_website_block_leaves_it_on(self, monkeypatch):
        """A config file with no ``website:`` key is the prod case."""
        monkeypatch.delenv("SUROGATES_WEBSITE_ENABLED", raising=False)
        from surogates.config import Settings

        assert Settings().website.enabled is True
