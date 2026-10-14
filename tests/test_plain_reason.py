"""Every reason the runtime records reads in the business's own wording."""

import pytest

from surogates.followup import outreach, store

RECORDED = {
    "Program has no opener message": "why_no_opener",
    "No whatsapp identity for this person": "why_no_number",
    "No telegram identity": "why_no_number",
    "No permission to message": "why_no_permission",
    "Permission withdrawn before the opener was sent": "why_no_permission",
    "No reachable channel": "why_no_channel",
    "The platform cannot write first on web": "why_no_channel",
    "No shared destination on whatsapp": "why_no_channel",
    "The person has not written to this telegram sender yet, so it cannot write to them first":
        "why_not_written_first",
    "The recipient has not written to this telegram sender yet": "why_not_written_first",
    "Opener not sent within the response window": "why_not_in_time",
    "No delivery report by the deadline": "why_not_in_time",
    "This agent has no phone number that can call out": "why_no_phone_line",
    "This agent has no webhook to deliver to": "why_no_webhook",
    "No credential vault in this process": "why_setup",
    "Slack bot token is missing for this sender": "why_setup",
    "Slack opened no direct message for this member": "why_setup",
    "Program stopped before the opener was sent": "why_stopped",
    "Program has no approved template to open with": "why_no_template",
    "The recipient's 24-hour window is closed and no approved template is configured to reopen it":
        "why_no_template",
    "The reopening template was not sent: boom": "why_template_failed",
    "(#131047) Re-engagement message": "why_window_closed",
    "The recipient did not answer the template within 24 hours": "why_expired",
    "Slack: channel_not_found": "why_undelivered",
    "(#131026) Message undeliverable": "why_undelivered",
    "": "why_undelivered",
}


@pytest.mark.parametrize("raw,key", RECORDED.items())
def test_each_recorded_reason_has_its_wording(raw, key):
    assert outreach.plain_reason(raw) == outreach.DEFAULT_TEXTS[key]
    assert outreach.plain_reason(raw, {key: "în limba firmei"}) == "în limba firmei"


def test_unknown_provider_text_never_shows_through():
    assert "boom" not in outreach.plain_reason("Some provider said boom")


def test_api_is_an_enrollment_source():
    assert "api" in store.ENROLLMENT_SOURCES
