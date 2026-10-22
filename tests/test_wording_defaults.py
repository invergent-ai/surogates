"""The default wording the runtime reads, and how a team text is signed."""

from surogates.followup.wording import all_default_texts


def test_single_sentences_are_listed():
    listed = all_default_texts()
    for key in ("welcome_text", "missed_alert_text", "why_undelivered", "link_question",
                "escalation_taken", "notice_enrolled_by", "plan_limit_notice"):
        assert listed[key]


def test_no_default_wording_uses_an_em_dash():
    assert [key for key, text in all_default_texts().items() if "—" in text] == []


def test_the_label_footer_signs_team_texts():
    from surogates.followup.store import signed

    assert signed(" Paused ", "Ana") == "Paused\n\nAbout: Ana"
    assert signed("Paused", "Ana", {"label_footer": "[{label}]"}) == "Paused\n\n[Ana]"
