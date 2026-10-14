"""What each channel declares it can do when the platform writes first."""

from __future__ import annotations

from surogates.channels.capabilities import capabilities_for, initiating_platforms


def test_whatsapp_opens_with_a_template_inside_a_24h_window():
    caps = capabilities_for("whatsapp")
    assert caps.can_initiate and caps.opener == "template"
    assert caps.window_hours == 24 and caps.forms and caps.identity == "phone"
    assert caps.receipts == {"delivered", "read"}


def test_slack_and_telegram_open_with_text_and_telegram_needs_prior_contact():
    slack, telegram = capabilities_for("slack"), capabilities_for("telegram")
    assert slack.opener == telegram.opener == "text"
    assert slack.window_hours is None and not slack.forms
    assert telegram.needs_prior_contact and not slack.needs_prior_contact


def test_channels_without_an_adapter_cannot_write_first():
    for kind in ("web", "website", "voice", None, ""):
        assert not capabilities_for(kind).can_initiate
    assert set(initiating_platforms()) == {"whatsapp", "slack", "telegram"}
