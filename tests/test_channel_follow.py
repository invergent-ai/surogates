from types import SimpleNamespace

from surogates.channels.channel_follow import channel_follow_enabled


def _session(channel, config):
    return SimpleNamespace(channel=channel, config=config)


def test_disabled_by_default():
    assert channel_follow_enabled(_session("slack", {})) is False


def test_enabled_when_config_flag_set():
    assert channel_follow_enabled(_session("slack", {"mate_follow": True})) is True


def test_non_slack_channel_never_follows():
    assert channel_follow_enabled(_session("web", {"mate_follow": True})) is False


def test_missing_config_is_safe():
    assert channel_follow_enabled(_session("slack", None)) is False
