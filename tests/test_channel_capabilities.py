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


def test_teams_writes_text_into_a_conversation_someone_opened():
    teams = capabilities_for("teams")
    assert teams.can_initiate and teams.needs_prior_contact and teams.opener == "text"
    assert teams.identity == "member" and teams.window_hours is None and not teams.forms
    assert teams.label == "Microsoft Teams"


def test_anonymous_website_visitors_cannot_be_written_to_first():
    for kind in ("website", None, ""):
        assert not capabilities_for(kind).can_initiate


def test_voice_web_and_api_write_first_into_the_persons_own_conversation():
    voice, web, api = capabilities_for("voice"), capabilities_for("web"), capabilities_for("api")
    assert voice.can_initiate and web.can_initiate and api.can_initiate
    assert voice.opener == web.opener == api.opener == "text"
    assert voice.window_hours is web.window_hours is api.window_hours is None
    # answered is delivered at once; only an acknowledged alert is read
    assert voice.identity == "phone" and voice.receipts == {"read"}
    assert web.identity == "user" and web.receipts == frozenset()
    assert api.identity == "external" and api.receipts == {"delivered", "read"}
    assert set(initiating_platforms()) == {
        "whatsapp", "slack", "teams", "telegram", "voice", "web", "api",
    }
