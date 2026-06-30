"""The single source of truth for resolving a session's channel platform —
ambient sessions are Slack-context, so they resolve to ``"slack"``."""

from types import SimpleNamespace

from surogates.channels.platform_resolve import effective_channel_platform


def test_ambient_resolves_to_slack():
    assert effective_channel_platform(SimpleNamespace(channel="ambient")) == "slack"


def test_slack_stays_slack():
    assert effective_channel_platform(SimpleNamespace(channel="slack")) == "slack"


def test_other_platform_passes_through():
    assert (
        effective_channel_platform(SimpleNamespace(channel="telegram")) == "telegram"
    )
    assert effective_channel_platform(SimpleNamespace(channel="web")) == "web"


def test_missing_channel_is_empty_string():
    assert effective_channel_platform(SimpleNamespace(channel=None)) == ""
    assert effective_channel_platform(SimpleNamespace()) == ""
